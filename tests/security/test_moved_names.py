"""Error classes `grelmicro.security.bans` and `.jwt` served in 0.43 are gone.

Each lives in `grelmicro.errors`. Reading it from the security module raises
`AttributeError` like any missing attribute.
"""

from __future__ import annotations

import importlib
import warnings

import pytest

import grelmicro.errors

_CASES = [
    ("grelmicro.security.bans", "AdmissionError"),
    ("grelmicro.security.jwt", "GrelmicroError"),
]
"""Each error class a security module served in 0.43, by module."""


@pytest.mark.parametrize(("module", "name"), _CASES)
def test_moved_name_raises_attribute_error(module: str, name: str) -> None:
    """Reading the name raises the plain `AttributeError` and warns nothing."""
    # Arrange
    served = importlib.import_module(module)

    # Act / Assert
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(
            AttributeError,
            match=f"^module '{module}' has no attribute '{name}'$",
        ):
            getattr(served, name)
    assert hasattr(grelmicro.errors, name)


@pytest.mark.parametrize(("module", "name"), _CASES)
def test_moved_name_stays_off_the_public_surface(
    module: str, name: str
) -> None:
    """The name is not in `__all__`, `dir()` or the module namespace."""
    # Arrange
    served = importlib.import_module(module)

    # Assert
    assert name not in served.__all__
    assert name not in dir(served)
    assert name not in vars(served)


@pytest.mark.parametrize("module", [module for module, _ in _CASES])
def test_the_module_serves_no_name_through_getattr(module: str) -> None:
    """The module has no `__getattr__` left to serve a name it does not define."""
    # Arrange
    served = importlib.import_module(module)

    # Assert
    assert "__getattr__" not in vars(served)
