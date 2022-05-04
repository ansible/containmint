# Copyright (C) 2022 Matt Clay
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)
"""Common fixtures for pytest."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope='session')
def environment() -> None:
    """Configure environment variables."""
    os.environ.update(
        COVERAGE_FILE=os.path.join(os.path.dirname(__file__), '../.coverage/coverage'),
    )

    for name in os.environ:
        if name.startswith('CONTAINMINT_'):
            del os.environ[name]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply markers to test functions based on fixture usage."""
    marks = (
        'config',
        'credentials',
        'remote',
    )

    for item in items:
        if isinstance(item, pytest.Function):  # pragma: no branch
            for fixture_name in item.fixturenames:
                if fixture_name in marks:
                    item.add_marker(fixture_name)
