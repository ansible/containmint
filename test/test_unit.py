# Copyright (C) 2022 Matt Clay
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Unit testing with pytest."""

from __future__ import annotations

import shutil
import tempfile
import unittest.mock

import pytest

import containmint


def test_version():
    """Make sure version is set."""
    assert containmint.__version__


def test_execute_serialization():
    """Test serialization of the execute command."""
    before = containmint.Execute(
        username='my_username',
        password='my_password',
        context='my_context',
        tag='my_tag',
        push=False,
        login=False,
    )

    with tempfile.NamedTemporaryFile(prefix='config-', suffix='.json') as config_file:
        before.serialize(config_file.name)
        after = containmint.Execute.deserialize(config_file.name)

    assert before == after


def test_program_not_found_error():
    """Make sure the correct exception is raised when a program is not found."""
    program = 'test/does/not/exist'

    with pytest.raises(containmint.ProgramNotFoundError) as ex:
        containmint.run_command(program)

    assert ex.value.name == program


def test_subprocess_error():
    """Make sure the correct exception is raised when a subprocess fails."""
    args = ('sh', '-c', 'echo out && echo err 1>&2 && exit 3')

    with pytest.raises(containmint.SubprocessError) as ex:
        containmint.run_command(*args, capture=True)

    assert ex.value.result.command == list(args)
    assert ex.value.result.status == 3
    assert ex.value.result.stdout == 'out\n'
    assert ex.value.result.stderr == 'err\n'


def test_no_container_engine_error():
    """Make sure the case of no container engine is properly handled."""

    with unittest.mock.patch.object(shutil, 'which', side_effect=lambda cmd: None) as patch:
        with pytest.raises(containmint.NoContainerEngineDetectedError):
            containmint.engine.detect()

        patch.assert_called()
