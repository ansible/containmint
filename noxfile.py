# Copyright (C) 2022 Matt Clay
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Testing with nox."""

from __future__ import annotations

import fnmatch
import os
import pathlib
import re
import shutil
import subprocess
import typing as t

import nox

updater: list[str] = []
"""Sessions which update the source code."""
checker: list[str] = []
"""Sessions which check the source code."""
special: list[str] = []
"""Sessions which have their own requirements but are not in the updater or checker list."""

PIP_OPTIONS = ('--disable-pip-version-check',)
F = t.TypeVar("F", bound=t.Callable[..., t.Any])


def track(tracker: list[str], name: str | None = None) -> t.Callable[[F], F]:
    """Record the decorated function with the given tracker."""

    def impl(func: F) -> F:
        """Record the decorated function."""
        tracker.append(name or func.__name__)
        return func

    return impl


@nox.session(reuse_venv=True)
def reformat(session: nox.Session) -> None:
    """Reformat code."""
    for target in updater:
        session.notify(target, posargs=[Helper.REFORMAT])


@nox.session()
def freeze(session: nox.Session) -> None:
    """Freeze test requirements."""
    for target in updater + checker + special:
        venv_path = pathlib.Path(session.bin).parent.parent.joinpath(target)

        if venv_path.is_dir():
            shutil.rmtree(venv_path)

        session.notify(target, posargs=[Helper.FREEZE])


@nox.session(reuse_venv=True, name='nox')
@track(special, name='nox')
def _nox(session: nox.Session) -> None:
    """Freeze nox requirements."""
    session.posargs.append(Helper.FREEZE)
    Helper(session)


@nox.session(reuse_venv=True)
@track(updater)
def black(session: nox.Session) -> None:
    """Run black."""
    helper = Helper(session)
    session.run('black', *([] if helper.update else ['--check']), *helper.find('*.py'))


@nox.session(reuse_venv=True)
@track(updater)
def isort(session: nox.Session) -> None:
    """Run isort."""
    helper = Helper(session)
    session.run('isort', *([] if helper.update else ['--check-only']), *helper.find('*.py'))


@nox.session(reuse_venv=True)
@track(updater)
def docformatter(session: nox.Session) -> None:
    """Run docformatter."""
    helper = Helper(session)
    options = ['--pre-summary-newline', '--wrap-summaries', '0', '--wrap-descriptions', '0']
    session.run('docformatter', *(['--in-place'] if helper.update else ['--check']), *options, *helper.find('*.py'))


@nox.session(reuse_venv=True)
@track(checker)
def pylint(session: nox.Session) -> None:
    """Run pylint."""
    helper = Helper(session)
    session.run('pylint', *helper.find('*.py'))


@nox.session(reuse_venv=True)
@track(checker)
def mypy(session: nox.Session) -> None:
    """Run mypy."""
    helper = Helper(session)
    session.run('mypy', *helper.find('*.py'))


@nox.session(reuse_venv=True)
@track(checker)
def yamllint(session: nox.Session) -> None:
    """Run yamllint."""
    helper = Helper(session)
    session.run('yamllint', '--strict', *helper.find('*.yml'))


@nox.session(reuse_venv=True)
@track(checker)
def doc8(session: nox.Session) -> None:
    """Run doc8."""
    helper = Helper(session)
    session.run('doc8', *helper.find('*.rst'))


@nox.session(reuse_venv=True)
@track(checker)
def rstcheck(session: nox.Session) -> None:
    """Run rstcheck."""
    helper = Helper(session)
    session.run('rstcheck', *helper.find('*.rst'))


@nox.session(reuse_venv=True)
@track(special)
def pytest(session: nox.Session) -> None:
    """Run pytest."""
    helper = Helper(session)

    process = subprocess.run([os.path.join(session.bin, 'python'), '-c', 'import ansible'], stdin=subprocess.DEVNULL, capture_output=True, check=False)

    if process.returncode:
        session.install(*PIP_OPTIONS, '-e', '.', silent=False)

    env = dict(
        CONTAINMINT_='',  # ensure the environment fixture always has something to delete
    )

    session.run('coverage', 'erase')
    session.run('coverage', 'run', '-m', 'pytest', *helper.args, env=env)
    session.run('coverage', 'combine')
    session.run('coverage', 'html')
    session.run('coverage', 'report')


@nox.session(reuse_venv=True)
@track(special)
def build(session: nox.Session) -> None:
    """Build the package."""
    Helper(session)

    dist = pathlib.Path('dist')

    if dist.is_dir():
        shutil.rmtree(dist)

    session.run('flit', 'build')


@nox.session(reuse_venv=False)
def validate(session: nox.Session) -> None:
    """Run pytest using the built wheel."""
    wheels = tuple(pathlib.Path('dist').glob('*.whl'))

    if len(wheels) != 1:
        session.error(f'Found {len(wheels)} wheels instead of 1. Did you run the build session first?')

    wheel = str(wheels[0])

    helper = Helper(session)
    helper.install('pytest')

    session.install(*PIP_OPTIONS, wheel, silent=False)

    pytest(session)


@nox.session(reuse_venv=True, name='all')
def _all(session: nox.Session) -> None:
    """Run all tests (default, build, validate)."""
    for target in nox.options.sessions:
        session.notify(target)

    session.notify('build')
    session.notify('validate')


class Helper:
    """Session helper."""

    FREEZE = '--freeze'
    REFORMAT = '--reformat'

    def __init__(self, session: nox.Session) -> None:
        self.session = session
        self.install()
        self.args = [arg for arg in self.session.posargs if arg not in (self.FREEZE, self.REFORMAT)]
        self.update = self.REFORMAT in self.session.posargs

    def install(self, name: str | None = None) -> None:
        """Invoke pip to install a packages for this session."""
        path = f'test/requirements/{name or self.session.name}'
        requirements_path = f'{path}.in'
        freeze_path = f'{path}.txt'

        if os.path.isfile(requirements_path):
            if self.FREEZE in self.session.posargs:
                self.session.install('-r', requirements_path, *PIP_OPTIONS, silent=False)

                pip = os.path.join(self.session.bin, 'pip')

                with open(freeze_path, 'wb') as freeze_file:
                    subprocess.run([pip, 'freeze', *PIP_OPTIONS], check=True, stdin=subprocess.DEVNULL, stdout=freeze_file, stderr=subprocess.PIPE)
            else:
                self.session.install('-r', freeze_path, *PIP_OPTIONS)

        if self.FREEZE in self.session.posargs:
            self.session.skip()

    def find(self, pattern: str) -> list[str]:
        """Return a list of paths matching the following pattern after excluding unwanted paths."""
        dir_exceptions = ('.azure-pipelines',)

        ignore_dir_patterns = [
            re.compile(pattern)
            for pattern in (
                r'^\.',
                r'^__',
                r'^dist$',
            )
        ]

        paths = []

        for dir_path, dir_names, file_names in os.walk('.', topdown=True):
            ignore_dirs = [
                dir_name for dir_name in dir_names if any(pattern.search(dir_name) for pattern in ignore_dir_patterns) and dir_name not in dir_exceptions
            ]

            for ignore_dir in ignore_dirs:
                dir_names.remove(ignore_dir)

            paths.extend(fnmatch.filter((os.path.join(dir_path[2:], file_name) for file_name in file_names), pattern))

        paths = sorted(paths)

        self.session.debug(f'{pattern} -> {" ".join(paths)}')

        return paths


nox.options.sessions = updater + checker
