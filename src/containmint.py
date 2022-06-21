#!/usr/bin/env python
# Copyright (C) 2022 Matt Clay
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
# PYTHON_ARGCOMPLETE_OK
"""Create multi-arch containers using native cloud builds."""

from __future__ import annotations

import abc
import argparse
import contextlib
import dataclasses
import itertools
import json
import os
import pathlib
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import typing as t

try:
    import argcomplete
except ImportError:
    argcomplete = None

__version__ = '0.2.0'

PROGRAM_NAME = os.path.basename(__file__)
TAG_FORMAT = '{server}/{repo}:{tag}'
CONFIG_NAME = 'config.json'
CONTEXT_NAME = 'context'
CONTAINER_FILES = ('Containerfile', 'Dockerfile')
UTF8 = 'utf-8'
INVENTORY_HOST = 'testhost'
ENGINE_LINK = '/tmp/containmint-engine'


@dataclasses.dataclass(frozen=True)  # type: ignore  # mypy bug: https://github.com/python/mypy/issues/5374
class Command(metaclass=abc.ABCMeta):
    """Base class for CLI commands."""

    @classmethod
    def cli_name(cls) -> str:
        """Name of the command on the CLI."""
        return cls.__name__.lower()

    @abc.abstractmethod
    def run(self) -> None:
        """Run the CLI command."""


@dataclasses.dataclass(frozen=True)  # type: ignore  # mypy bug: https://github.com/python/mypy/issues/5374
class BuildCommand(Command, metaclass=abc.ABCMeta):
    """Base class for CLI build related commands."""

    context: str
    tag: str
    push: bool
    login: bool


@dataclasses.dataclass(frozen=True)
class Build(BuildCommand):
    """Build and push an image using a remote instance."""

    keep_instance: bool
    remote: str
    arch: str

    def run(self) -> None:
        """Run the CLI command."""

        workdir_name = f'workdir-{secrets.token_hex(4)}'
        remote_workdir = f'/root/{workdir_name}'
        remote_program = os.path.join(remote_workdir, PROGRAM_NAME)

        execute = Execute(
            context=os.path.join(remote_workdir, CONTEXT_NAME),
            tag=self.tag,
            login=self.login,
            push=self.push,
        )

        ansible_test_shell = ('ansible-test', 'shell', '--target-posix', f'remote:{self.remote},arch={self.arch}', '--color', '-v', '--truncate', '0')

        with tempfile.NamedTemporaryFile(prefix='remote-', suffix='.inventory') as inventory_file:
            run_command(*ansible_test_shell, '--export', inventory_file.name)
            process = run_command('ansible-inventory', '-i', inventory_file.name, '--host', INVENTORY_HOST, capture=True)
            inventory_vars = json.loads(process.stdout)
            remote_python_interpreter = inventory_vars['ansible_python_interpreter']

            self.upload_payload(remote_workdir=remote_workdir, workdir_name=workdir_name, inventory=inventory_file.name, execute=execute)

            options = []

            if not self.keep_instance:
                options.extend(['--remote-terminate', 'always'])

            run_command(*ansible_test_shell, *options, '--raw', '--', remote_python_interpreter, remote_program, Dispatch.cli_name())

    def upload_payload(self, remote_workdir: str, workdir_name: str, inventory: str, execute: Execute) -> None:
        """
        Generate and upload the payload to the remote host.

        The payload contains this program, the build configuration and the build context.
        """
        ansible_env = os.environ.copy()
        ansible_env.update(
            ANSIBLE_DEVEL_WARNING='no',
            ANSIBLE_HOST_KEY_CHECKING='no',
            ANSIBLE_FORCE_COLOR='yes',
        )

        with tempfile.NamedTemporaryFile(prefix='config-', suffix='.json') as config_file:
            execute.serialize(config_file.name)

            with tempfile.NamedTemporaryFile(prefix='content-', suffix='.tgz') as archive_file:
                with tarfile.open(archive_file.name, "w:gz") as tar:
                    tar.add(__file__, arcname=os.path.join(workdir_name, PROGRAM_NAME))
                    tar.add(config_file.name, arcname=os.path.join(workdir_name, CONFIG_NAME))
                    tar.add(self.context, arcname=os.path.join(workdir_name, CONTEXT_NAME))

                module_args = dict(
                    src=archive_file.name,
                    dest=os.path.dirname(remote_workdir),
                )

                ansible_options = {
                    '-m': 'unarchive',
                    '-a': ' '.join(f'{key}={value}' for key, value in module_args.items()),
                    '-i': inventory,
                }

                ansible_unarchive = ['ansible'] + list(itertools.chain.from_iterable(ansible_options.items())) + [INVENTORY_HOST]

                run_command(*ansible_unarchive, env=ansible_env, capture=True)


@dataclasses.dataclass(frozen=True)
class Execute(BuildCommand):
    """
    Execute a local build and push.

    Internal use only.
    """

    username: str = ''
    password: str = ''

    def __post_init__(self):
        """Populate username and password from the environment if they are not already set."""
        if self.username and self.password:
            return

        if not self.login:
            return

        credentials = RegistryCredentials.create()

        object.__setattr__(self, 'username', credentials.username)
        object.__setattr__(self, 'password', credentials.password)

    @classmethod
    def deserialize(cls, config_path: str) -> Execute:
        """Deserialize an instance from the specified config path."""
        try:
            with open(config_path, encoding=UTF8) as config_file:
                config = json.load(config_file)
        except Exception as ex:
            raise ConfigurationError(str(ex)) from ex

        execute = cls(**config)

        return execute

    def serialize(self, config_path: str) -> None:
        """Serialize this instance to the specified config path."""
        with open(config_path, 'w', encoding=UTF8) as config_file:
            json.dump(self.__dict__, config_file)

    def run(self) -> None:
        """Run the CLI command."""
        image = ImageReference.parse(self.tag)

        paths = [os.path.join(self.context, name) for name in CONTAINER_FILES]
        match = [path for path in paths if os.path.isfile(path)][0]

        credentials = RegistryCredentials(self.username, self.password) if self.login else None

        options = ('--format', 'docker') if engine.program.name == 'podman' else ()

        with registry_login(image.server, credentials):
            engine.run('build', '--tag', self.tag, '--file', match, self.context, '--no-cache', *options)

            if self.push:
                engine.run('push', self.tag)


@dataclasses.dataclass(frozen=True)
class Merge(Command):
    """Create and push a manifest list."""

    tags: list[str]
    sources: list[str]
    push: bool
    login: bool

    def run(self) -> None:
        """Run the CLI command."""
        server = get_server(self.tags + self.sources)
        credentials = RegistryCredentials.create() if self.login else None
        suppress = (self.ManifestPushError,) if self.should_enable_workaround else ()

        with registry_login(server, credentials):
            with contextlib.suppress(*suppress):
                self.merge()
                return

            self.apply_work_around()
            self.merge()

    def merge(self) -> None:
        """Create and push manifest lists."""
        for tag in self.tags:
            with contextlib.suppress(SubprocessError):
                engine.run('manifest', 'rm', tag)

            engine.run('manifest', 'create', tag, *self.sources)

            if self.push:
                options = (f'docker://{tag}',) if engine.program.name == 'podman' else ()

                try:
                    engine.run('manifest', 'push', tag, *options)
                except SubprocessError as ex:
                    raise self.ManifestPushError() from ex

    class ManifestPushError(Exception):
        """An error occurred while pushing a manifest."""

    @property
    def should_enable_workaround(self):
        """
        Special handling may be required in some cases when pushing a manifest list to a different repository than the referenced images reside in.

        When this occurs, the container engine first pushes the images to the repository where the manifest list will be pushed.
        These newly pushed images may have a different digest from the original images.
        However, because the manifest list was created using the original images, it will contain the original digests.
        If the digests differ, the manifest list push will fail because it references digests which do not exist.
        """
        return self.push and len(set(ImageReference.parse(tag).full_repo for tag in self.tags + self.sources)) > 1

    def apply_work_around(self) -> None:
        """
        Apply a work-around for errors caused by using multiple repositories.

        Pull the source images and re-push them to update their digests.
        The updated image digests should then be the same as those which will be pushed to the repository where the manifest list will be pushed.
        """
        display.warning('Applying work-around for digest mismatch when using multiple repositories.')

        for source in self.sources:
            engine.run('pull', source)
            engine.run('push', source)


@dataclasses.dataclass(frozen=True)
class Dispatch(Command):
    """
    Execute a local build and push using a config file.

    Internal use only.
    """

    def run(self) -> None:
        """Run the CLI command."""
        workdir = os.path.dirname(__file__)
        config = os.path.join(workdir, 'config.json')

        run_command('uname', '-a')

        execute = Execute.deserialize(config)

        bootstrapper = Bootstrapper.init()
        bootstrapper.run()

        with contextlib.suppress(FileExistsError):
            # ease integration testing by leaving behind a link to the container engine
            os.symlink(engine.program, ENGINE_LINK)

        execute.run()


class ContainerEngine:
    """Container engine abstraction."""

    def __init__(self, program: pathlib.Path | None = None) -> None:
        self._program = program

    @property
    def program(self) -> pathlib.Path:
        """The program used to manage containers."""
        if not self._program:
            self._program = self.detect()
            self.run('--version')

        return self._program

    def run(
        self,
        *command: str,
        data: str | None = None,
        stdin: int | t.IO[bytes] | None = None,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> SubprocessResult:
        """Run the specified container management command."""
        return run_command(self.program.name, *command, data=data, stdin=stdin, env=env, capture=capture)

    @staticmethod
    def detect() -> pathlib.Path:
        """Detect the program used to manage containers."""
        programs = ('podman', 'docker')

        for program in programs:
            if path := shutil.which(program):
                return pathlib.Path(path)

        raise NoContainerEngineDetectedError(programs)


@dataclasses.dataclass(frozen=True)
class ImageReference:
    """Parsed image reference."""

    server: str
    repo: str
    tag: str

    @property
    def full_repo(self):
        """The repo name including the server component."""
        return f'{self.server}/{self.repo}'

    @classmethod
    def parse(cls, value: str) -> ImageReference:
        """Parse the given value and return an image reference."""
        server, name = value.split('/', 1)
        repo, tag = name.split(':', 1)

        ref = cls(
            server=server,
            repo=repo,
            tag=tag,
        )

        return ref


@dataclasses.dataclass(frozen=True)
class RegistryCredentials:
    """Container registry credentials."""

    username: str
    password: str

    def __post_init__(self):
        """Register password as a sensitive value."""
        display.sensitive.add(self.password)

    @classmethod
    def create(cls) -> RegistryCredentials:
        """Create and return credentials loaded from the environment."""
        try:
            username = os.environ['CONTAINMINT_USERNAME']
            password = os.environ['CONTAINMINT_PASSWORD']
        except KeyError as ex:
            raise MissingEnvVarError(ex.args[0]) from None

        credentials = cls(
            username=username,
            password=password,
        )

        return credentials


class Bootstrapper:
    """Bootstrapper for remote instances."""

    @classmethod
    def usable(cls) -> bool:
        """Return True if the bootstrapper can be used, otherwise False."""
        return False

    @staticmethod
    def run() -> None:
        """Run the bootstrapper."""

    @classmethod
    def init(cls) -> t.Type[Bootstrapper]:
        """Return a bootstrapper type appropriate for the current system."""
        for bootstrapper in cls.__subclasses__():
            if bootstrapper.usable():
                return bootstrapper

        display.warning('No supported bootstrapper found.')
        return Bootstrapper


class DnfBootstrapper(Bootstrapper):
    """Bootstrapper for dnf based systems."""

    @classmethod
    def usable(cls) -> bool:
        """Return True if the bootstrapper can be used, otherwise False."""
        return bool(shutil.which('dnf'))

    @staticmethod
    def run() -> None:
        """Run the bootstrapper."""
        run_command('dnf', 'install', '-y', 'podman')


class AptBootstrapper(Bootstrapper):
    """Bootstrapper for apt based systems."""

    @classmethod
    def usable(cls) -> bool:
        """Return True if the bootstrapper can be used, otherwise False."""
        return bool(shutil.which('apt-get'))

    @staticmethod
    def run() -> None:
        """Run the bootstrapper."""
        apt_env = os.environ.copy()
        apt_env.update(
            DEBIAN_FRONTEND='noninteractive',
        )

        run_command('apt-get', 'install', 'docker.io', '-y', '--no-install-recommends', env=apt_env)


@dataclasses.dataclass(frozen=True)
class SubprocessResult:
    """Result from execution of a subprocess."""

    command: list[str]
    stdout: str
    stderr: str
    status: int


class ApplicationError(Exception):
    """An application error."""

    def __init__(self, message: str) -> None:
        self.message = message

        super().__init__(message)


class NoContainerEngineDetectedError(ApplicationError):
    """No container engine was detected."""

    def __init__(self, programs: tuple[str, ...]) -> None:
        self.programs = programs

        super().__init__(f'Unable to detect one of the following container engines: {", ".join(programs)}')


class MultipleServersError(ApplicationError):
    """Multiple servers were specified."""

    def __init__(self, servers: tuple[str, ...]) -> None:
        self.servers = servers

        super().__init__(f'Multiple servers were specified when only one is supported: {", ".join(servers)}')


class ConfigurationError(ApplicationError):
    """Invalid configuration was provided."""

    def __init__(self, message: str) -> None:
        super().__init__(f'Configuration error: {message}')


class MissingEnvVarError(ApplicationError):
    """A required environment variable was not found."""

    def __init__(self, name: str) -> None:
        self.name = name

        super().__init__(f'Missing environment variable: {name}')


class ProgramNotFoundError(ApplicationError):
    """A required program was not found."""

    def __init__(self, name: str) -> None:
        self.name = name

        super().__init__(f'Missing program: {name}')


class SubprocessError(ApplicationError):
    """An error from executing a subprocess."""

    def __init__(self, result: SubprocessResult) -> None:
        self.result = result

        message = f'Command `{shlex.join(result.command)}` exited with status: {result.status}'

        stdout = (result.stdout or '').strip()
        stderr = (result.stderr or '').strip()

        if stdout:
            message += f'\n>>> Standard Output\n{stdout}'

        if stderr:
            message += f'\n>>> Standard Error\n{stderr}'

        super().__init__(message)


class Display:
    """Display interface for sending output to the console."""

    CLEAR = '\033[0m'
    RED = '\033[31m'
    BLUE = '\033[34m'
    PURPLE = '\033[35m'
    CYAN = '\033[36m'

    def __init__(self) -> None:
        self.sensitive: set[str] = set()

    def section(self, message: str) -> None:
        """Print a section message to the console."""
        self.show(f'==> {message}', color=self.BLUE)

    def subsection(self, message: str) -> None:
        """Print a subsection message to the console."""
        self.show(f'--> {message}', color=self.CYAN)

    def fatal(self, message: str) -> None:
        """Print a fatal message to the console."""
        self.show(f'FATAL: {message}', color=self.RED)

    def warning(self, message: str) -> None:
        """Print a warning message to the console."""
        self.show(f'WARNING: {message}', color=self.PURPLE)

    def show(self, message: str, color: str | None = None) -> None:
        """Print a message to the console."""
        for item in self.sensitive:
            message = message.replace(item, '*' * len(item))

        print(f'{color or self.CLEAR}{message}{self.CLEAR}', flush=True)


@contextlib.contextmanager
def registry_login(server: str, credentials: RegistryCredentials | None) -> t.Generator[None, None, None]:
    """Log in to a registry when entering the context and log out when exiting the context."""
    if credentials:
        engine.run('login', '--username', credentials.username, '--password-stdin', server, data=credentials.password, capture=True)

    try:
        yield
    finally:
        if credentials:
            engine.run('logout', server, capture=True)


def get_server(tags: list[str]) -> str:
    """
    Return the server from the given tag(s).

    Raise an exception if more than one server was found.
    """
    servers = tuple(sorted(set(ref.server for ref in (ImageReference.parse(tag) for tag in tags))))

    if len(servers) != 1:
        raise MultipleServersError(servers)

    server = servers[0]

    return server


def image_ref(value: str) -> str:
    """Validate an image reference."""
    try:
        ImageReference.parse(value)
    except Exception:
        raise argparse.ArgumentTypeError(f'required format is: {TAG_FORMAT}') from None

    return value


def context_ref(value: str) -> str:
    """Validate a context reference."""
    if not os.path.isdir(value):
        raise argparse.ArgumentTypeError(f'context must be a directory: {value}')

    paths = [os.path.join(value, name) for name in CONTAINER_FILES]
    matches = [path for path in paths if os.path.isfile(path)]

    if not matches:
        raise argparse.ArgumentTypeError(f'missing one of: {" ".join(paths)}')

    if len(matches) > 1:
        raise argparse.ArgumentTypeError(f'multiple matches: {" ".join(matches)}')

    return value


def run_command(
    *command: str,
    data: str | None = None,
    stdin: int | t.IO[bytes] | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> SubprocessResult:
    """Run the specified command and return the result."""
    stdin = subprocess.PIPE if data else stdin or subprocess.DEVNULL
    stdout = subprocess.PIPE if capture else None
    stderr = subprocess.PIPE if capture else None

    display.subsection(f'Run command: {shlex.join(command)}')

    try:
        with subprocess.Popen(args=command, stdin=stdin, stdout=stdout, stderr=stderr, env=env, text=True) as process:
            process_stdout, process_stderr = process.communicate(data)
            process_status = process.returncode
    except FileNotFoundError:
        raise ProgramNotFoundError(command[0]) from None

    result = SubprocessResult(
        command=list(command),
        stdout=process_stdout,
        stderr=process_stderr,
        status=process_status,
    )

    if process.returncode != 0:
        raise SubprocessError(result)

    return result


def parse_args() -> Command:
    """Parse CLI args and return a Command instance."""
    command_name = next(iter(sys.argv[1:]), None)  # detect command name so internal-use-only commands can be hidden

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')

    subparsers = parser.add_subparsers(metavar='COMMAND', required=True)

    common_parser = argparse.ArgumentParser(add_help=False)

    common_build_parser = argparse.ArgumentParser(add_help=False, parents=[common_parser])
    common_build_parser.add_argument('--tag', type=image_ref, required=True, help=f'image tag: {TAG_FORMAT}')
    common_build_parser.add_argument('--context', type=context_ref, default='.', help='path to the build context')
    common_build_parser.add_argument('--push', action='store_true', help='push the image')
    common_build_parser.add_argument('--no-login', action='store_false', dest='login', help='do not log in')

    build_parser = subparsers.add_parser(Build.cli_name(), parents=[common_build_parser], description=Build.__doc__, help=Build.__doc__)
    build_parser.add_argument('--keep-instance', action='store_true', help='keep the remote instance')
    build_parser.add_argument('--remote', default='ubuntu/22.04', help='ansible-test remote target args')
    build_parser.add_argument('--arch', metavar='ARCH', default='x86_64', choices=['x86_64', 'aarch64'], help='architecture (choices: %(choices)s)')
    build_parser.set_defaults(command_type=Build)

    merge_parser = subparsers.add_parser(Merge.cli_name(), parents=[common_parser], description=Merge.__doc__, help=Merge.__doc__)
    merge_parser.add_argument('--tag', type=image_ref, required=True, dest='tags', metavar='TAG', action='append', help=f'image tag: {TAG_FORMAT}')
    merge_parser.add_argument('--push', action='store_true', help='push the manifest')
    merge_parser.add_argument('--no-login', action='store_false', dest='login', help='do not log in')
    merge_parser.add_argument('sources', type=image_ref, metavar='source', nargs='+', help=f'source image tag: {TAG_FORMAT}')
    merge_parser.set_defaults(command_type=Merge)

    if command_name == Execute.cli_name():
        execute_parser = subparsers.add_parser(Execute.cli_name(), parents=[common_build_parser], description=Execute.__doc__, help=Execute.__doc__)
        execute_parser.set_defaults(command_type=Execute)

    if command_name == Dispatch.cli_name():
        dispatch_parser = subparsers.add_parser(Dispatch.cli_name(), parents=[common_parser], description=Dispatch.__doc__, help=Dispatch.__doc__)
        dispatch_parser.set_defaults(command_type=Dispatch)

    if argcomplete:
        argcomplete.autocomplete(parser)

    args = parser.parse_args()
    kwargs = {field.name: getattr(args, field.name) for field in dataclasses.fields(args.command_type) if hasattr(args, field.name)}
    command = args.command_type(**kwargs)

    return command


def main() -> None:
    """Main program entry point."""
    try:
        command = parse_args()
        display.section(f'Begin: {command.cli_name()}({command.__dict__})')
        command.run()
        display.section(f'End: {command.cli_name()}({command.__dict__})')
    except ApplicationError as ex:
        display.fatal(ex.message)
        sys.exit(1)


display = Display()
engine = ContainerEngine()

if __name__ == '__main__':
    main()
