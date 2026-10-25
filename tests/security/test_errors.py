"""Tests for the security error classes every import path shares."""

from __future__ import annotations

import importlib

import pytest

from grelmicro.security import _errors

_PUBLIC_PATHS = (
    ("grelmicro.security", "ClientBannedError"),
    ("grelmicro.security", "TokenRejectedError"),
    ("grelmicro.security", "TokenRejectedReason"),
    ("grelmicro.security.bans", "ClientBannedError"),
    ("grelmicro.security.jwt", "TokenRejectedError"),
    ("grelmicro.security.jwt", "TokenRejectedReason"),
)
"""Every public path an error class is imported from."""

_HOMES = {
    "ClientBannedError": "grelmicro.security.bans",
    "TokenRejectedError": "grelmicro.security.jwt",
    "TokenRejectedReason": "grelmicro.security.jwt",
}
"""The public module each error class reports as its own."""


@pytest.mark.parametrize(("module", "name"), _PUBLIC_PATHS)
def test_public_path_returns_the_same_class(module: str, name: str) -> None:
    """Every public path returns the one class an `except` clause catches."""
    # Act
    found = getattr(importlib.import_module(module), name)

    # Assert
    assert found is getattr(_errors, name)


@pytest.mark.parametrize(("name", "home"), sorted(_HOMES.items()))
def test_error_class_reports_its_public_module(name: str, home: str) -> None:
    """An error class names its public module, not the private one."""
    # Act
    cls = getattr(_errors, name)

    # Assert
    assert cls.__module__ == home
