# Copyright (C) 2022 Matt Clay
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Integration testing with pytest."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
import shlex
import subprocess
import time
import typing as t
import unittest.mock

import pytest

import containmint

ARCHITECTURES = (
    'x86_64',
    'aarch64',
)

REMOTES = (
    'ubuntu/22.04',
    'rhel/9.0',
)

SQUASH_TYPES = (None, 'all', 'new')


@dataclasses.dataclass(frozen=True)
class Config:
    """Test configuration."""

    scratch_repo: str
    final_repo: str

    def __post_init__(self):
        assert self.scratch_repo != self.final_repo  # manifest push behavior can change when images reside in a different repository

    def merge_tag(self, remote: str) -> str:
        """Return a final tag for merge tests to push from the specified remote."""
        return f'{self.final_repo}:test_merge-{make_tag(remote)}'

    def merge_sources(self, remote: str) -> dict[str, str]:
        """Return scratch sources for merge tests to use for each architecture."""
        return {self.build_tag(remote, arch): arch for arch in ARCHITECTURES}

    def build_tag(self, remote: str, arch: str) -> str:
        """Return a scratch tag for build tests to push from the specified remote using the given architecture."""
        return f'{self.scratch_repo}:test_build-{make_tag(remote)}-{arch}'

    @property
    def execute_tag(self) -> str:
        """Return a scratch tag for execute tests to push."""
        return f'{self.scratch_repo}:test_execute'


@pytest.fixture(name='config', scope='session')
def _config() -> Config:
    """Return test configuration."""
    try:
        return Config(
            scratch_repo=os.environ['TEST_SCRATCH_REPO'],
            final_repo=os.environ['TEST_FINAL_REPO'],
        )
    except KeyError as ex:  # pragma: no cover
        raise pytest.skip(f'Missing environment variable: {ex.args[0]}')


@dataclasses.dataclass(frozen=True)
class Credentials:
    """Test credentials."""

    username: str
    password: str

    @property
    def env(self) -> dict[str, str]:
        """Environment variables containing the credentials."""
        return dict(
            CONTAINMINT_USERNAME=self.username,
            CONTAINMINT_PASSWORD=self.password,
        )


@pytest.fixture(name='credentials', scope='session')
def _credentials() -> Credentials:
    """Return test credentials."""
    try:
        return Credentials(
            username=os.environ['TEST_USERNAME'],
            password=os.environ['TEST_PASSWORD'],
        )
    except KeyError as ex:  # pragma: no cover
        raise pytest.skip(f'Missing environment variable: {ex.args[0]}')


@pytest.mark.remote
def test_provision() -> None:
    """Provision remote instances in parallel to speed up test execution later."""
    errors = 0

    with contextlib.ExitStack() as stack:
        # noinspection PyTypeChecker
        jobs: dict[str, subprocess.Popen] = {}

        for remote in REMOTES:
            for arch in ARCHITECTURES:
                args = ('ansible-test', 'shell', '--target-posix', f'remote:{remote},arch={arch}', 'id')

                logging.debug('>>> %s', shlex.join(args))

                # noinspection PyTypeChecker
                jobs[f'{remote} ({arch})'] = stack.enter_context(
                    subprocess.Popen(
                        args,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )

        while jobs:
            time.sleep(1)

            for name, process in tuple(jobs.items()):
                if process.poll() is not None:
                    del jobs[name]

                    if process.returncode:  # pragma: no cover
                        logging.error('Instance failed (%d): %s', process.returncode, name)
                        errors += 1
                    else:
                        logging.debug('Instance ready: %s', name)

    assert not errors


@pytest.mark.parametrize('arch', ARCHITECTURES)
@pytest.mark.parametrize('remote', REMOTES, ids=[f'on:{remote}' for remote in REMOTES])
@pytest.mark.parametrize('squash', SQUASH_TYPES, ids=[f'squash:{squash_type}' for squash_type in SQUASH_TYPES])
def test_build(config: Config, credentials: Credentials, remote: str, arch: str, squash: t.Optional[str]) -> None:
    """Run the 'build' command with the '--push' option."""
    new_container_ctx = 'test/contexts/simple'
    new_container_file = os.path.join(new_container_ctx, 'Containerfile')
    new_container_own_layer_count = 3  # number of layers known to be created by the container (must be kept in sync with changes to the container file)

    assert new_container_own_layer_count > 1  # squash new requires 2+ layers to verify, this check helps avoid mistakes when updating the container file

    tag = config.build_tag(remote, arch)

    squash_args = ('--squash', squash) if squash else ()

    # HACK: expose the engine in use so we can properly probe for squash support
    squash_supported = 'rhel' in remote

    # if we expect squash to fail, wire up an assertion to verify, otherwise a no-op nullcontext
    err_assert = t.cast(t.ContextManager, pytest.raises(subprocess.CalledProcessError)) if squash and not squash_supported else contextlib.nullcontext()

    with unittest.mock.patch.dict(os.environ, credentials.env), err_assert as err_context:
        run_containmint('build', '--tag', tag, '--arch', arch, '--remote', remote, '--context', new_container_ctx, '--push', '--keep-instance', *squash_args)

    # if the remote process failed, poke at the output (merged to stdout) to ensure it failed for the right reason
    if err_context:
        assert f'does not support squash mode {squash}' in err_context.value.stdout

    # validate non-zero-size layer counts against base image to ensure the squash (or lack thereof) resulted in the expected number of layers
    if not squash or squash_supported:
        local_engine = containmint.engine.program

        proc = run(str(local_engine), 'history', '--format', '{{json .}}', '--human=false', get_base_image_from_container_file(new_container_file))
        data = f'[{",".join(proc.stdout.splitlines())}]' if local_engine.name == 'docker' else proc.stdout
        base_layer_count = len([layer for layer in json.loads(data) if layer.get('size', int(layer.get('Size', 0))) > 0])

        proc = run(str(local_engine), 'manifest', 'inspect', *(('--log-level=error',) if local_engine.name == 'podman' else ()), tag)
        layer_count = len([layer for layer in json.loads(proc.stdout)['layers'] if layer.get('size', 0) > 0])

        if squash == 'new':
            assert layer_count == base_layer_count + 1
        elif squash == 'all':
            assert layer_count == 1
        else:
            assert layer_count == base_layer_count + new_container_own_layer_count


@pytest.mark.parametrize('remote', REMOTES, ids=[f'from:{remote}' for remote in REMOTES])
def test_merge_no_login(config: Config, remote: str) -> None:
    """Run the 'merge' command using already pushed images with the '--no-login' option."""
    tag = config.merge_tag(remote)
    sources = config.merge_sources(remote)

    run_containmint('merge', '--tag', tag, '--no-login', *sources)


@pytest.mark.parametrize('remote', REMOTES, ids=[f'from:{remote}' for remote in REMOTES])
def test_merge(config: Config, credentials: Credentials, remote: str) -> None:
    """Run the 'merge' command using already pushed images with the '--push' option."""
    tag = config.merge_tag(remote)
    sources = config.merge_sources(remote)

    with unittest.mock.patch.dict(os.environ, credentials.env):
        run_containmint('merge', '--tag', tag, '--push', *sources)


@pytest.mark.parametrize('builder', REMOTES, ids=[f'from:{remote}' for remote in REMOTES])
@pytest.mark.parametrize('arch', ARCHITECTURES)
@pytest.mark.parametrize('remote', REMOTES, ids=[f'on:{remote}' for remote in REMOTES])
def test_matrix(config: Config, builder: str, remote: str, arch: str) -> None:
    """Test the 'merge' command result created by a builder using a specific remote and architecture."""
    tag = config.merge_tag(builder)

    ansible_test_shell = ('ansible-test', 'shell', '--target-posix', f'remote:{remote},arch={arch}', '--color', '-v', '--truncate', '0', '--raw', '--')

    # use the shortcut left behind by the execute command
    run(*ansible_test_shell, containmint.ENGINE_LINK, 'run', tag, 'uname', '-a')


def test_execute(config: Config, credentials: Credentials) -> None:
    """Run the 'execute' command with the '--push' option."""
    scratch = config.execute_tag

    with unittest.mock.patch.dict(os.environ, credentials.env):
        run_containmint('execute', '--tag', scratch, '--context', 'test/contexts/simple', '--push')


def test_execute_no_login() -> None:
    """Run the 'execute' command with the '--no-login' option."""
    run_containmint('execute', '--tag', 'example.com/repo/name:latest', '--context', 'test/contexts/simple', '--no-login')


def test_execute_empty_context_error() -> None:
    """Run the 'execute' command with an empty context."""
    with pytest.raises(subprocess.CalledProcessError) as ex:
        run_containmint('execute', '--tag', 'example.com/repo/name:latest', '--context', 'test/contexts/empty')

    assert ex.value.returncode == 2
    assert 'missing one of: ' in ex.value.stdout


def test_execute_conflicting_context_error() -> None:
    """Run the 'execute' command with a conflicting context."""
    with pytest.raises(subprocess.CalledProcessError) as ex:
        run_containmint('execute', '--tag', 'example.com/repo/name:latest', '--context', 'test/contexts/conflicting')

    assert ex.value.returncode == 2
    assert 'multiple matches: ' in ex.value.stdout


def test_execute_file_context_error() -> None:
    """Run the 'execute' command with a file as the context."""
    with pytest.raises(subprocess.CalledProcessError) as ex:
        run_containmint('execute', '--tag', 'example.com/repo/name:latest', '--context', 'test/contexts/simple/Containerfile')

    assert ex.value.returncode == 2
    assert 'context must be a directory: ' in ex.value.stdout


def test_execute_invalid_credentials_error(config: Config) -> None:
    """Run the 'execute' command with invalid credentials."""
    credentials = Credentials(username='invalid', password='invalid')

    with unittest.mock.patch.dict(os.environ, credentials.env):
        with pytest.raises(subprocess.CalledProcessError) as ex:
            run_containmint('execute', '--tag', config.execute_tag, '--context', 'test/contexts/simple')

    assert ex.value.returncode == 1
    assert 'username' in ex.value.stdout.lower()
    assert 'password' in ex.value.stdout.lower()


def test_execute_missing_credentials_error() -> None:
    """Run the 'execute' command with missing credentials."""
    with pytest.raises(subprocess.CalledProcessError) as ex:
        run_containmint('execute', '--tag', 'example.com/repo/name:latest', '--context', 'test/contexts/simple')

    assert ex.value.returncode == 1
    assert 'Missing environment variable: ' in ex.value.stdout


def test_execute_invalid_tag_error() -> None:
    """Run the 'execute' command with an invalid tag."""
    with pytest.raises(subprocess.CalledProcessError) as ex:
        run_containmint('execute', '--tag', 'invalid', '--context', 'test/contexts/simple')

    assert ex.value.returncode == 2
    assert 'required format is: ' in ex.value.stdout


def test_dispatch_no_config_error() -> None:
    """Run the 'dispatch' command without a config file present."""
    with pytest.raises(subprocess.CalledProcessError) as ex:
        run_containmint('dispatch')

    assert ex.value.returncode == 1
    assert 'Configuration error: ' in ex.value.stdout


def test_merge_multiple_servers_error() -> None:
    """Run the 'merge' command with multiple servers."""
    with pytest.raises(subprocess.CalledProcessError) as ex:
        run_containmint('merge', '--tag', 'example.com/repo/name:latest', 'example.net/repo/name:latest')

    assert ex.value.returncode == 1
    assert 'Multiple servers were specified when only one is supported: example.com, example.net' in ex.value.stdout


def run(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run the specified command."""
    logging.debug('>>> %s', shlex.join(args))

    stdout = []

    try:
        with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd) as process:
            assert process.stdout

            while line := process.stdout.readline():
                text = line.decode().rstrip()
                logging.info('%s', text)
                stdout.append(f'{text}\n')

            process.wait()
    except FileNotFoundError:  # pragma: no cover
        raise Exception(f'Program not found: {args[0]}') from None

    if process.returncode != 0:
        raise subprocess.CalledProcessError(
            cmd=process.args,
            returncode=process.returncode,
            output=''.join(stdout),
        )

    return subprocess.CompletedProcess(
        args=args,
        returncode=process.returncode,
        stdout=''.join(stdout),
    )


def run_containmint(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run the specified containmint command while collecting code coverage."""
    return run('coverage', 'run', '-m', 'containmint', *args, cwd=cwd)


def make_tag(value: str) -> str:
    """Return the given value with substitutions performed to make it suitable for use in a tag."""
    return re.sub('[^a-zA-Z0-9_.-]+', '-', value)


def get_base_image_from_container_file(path: str) -> str:
    """Return the first image ref FROM base image from the specified container file."""
    img_re = re.compile(r'^FROM (.+)$', flags=re.MULTILINE)

    with open(path, 'r', encoding='UTF-8') as reader:
        match = img_re.search(reader.read())

    assert match
    return match.group(1)
