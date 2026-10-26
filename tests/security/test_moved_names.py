"""Error classes `grelmicro.security.bans` and `.jwt` served in 0.43.

Each still loads from `grelmicro.errors`, with a warning until 0.45.0.
"""

from __future__ import annotations

import importlib

import pytest

import grelmicro.errors
from grelmicro._deprecation import FACADE_MOVES, SECURITY_MOVED
from grelmicro.errors import Grelmicro045Warning

_CASES = [
    (moved.module, name) for moved in SECURITY_MOVED for name in moved.homes
]


@pytest.mark.parametrize(("module", "name"), _CASES)
def test_moved_name_loads_from_errors_with_a_warning(
    module: str, name: str
) -> None:
    """The name is the class from `grelmicro.errors`, and reading it warns."""
    # Arrange
    served = importlib.import_module(module)

    # Act
    with pytest.warns(
        Grelmicro045Warning, match=FACADE_MOVES[module].message_for(name)
    ) as caught:
        value = getattr(served, name)

    # Assert
    assert value is getattr(grelmicro.errors, name)
    assert caught[0].filename == __file__


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


@pytest.mark.parametrize("module", [moved.module for moved in SECURITY_MOVED])
def test_an_unknown_name_still_raises(module: str) -> None:
    """A name the module never served raises `AttributeError`."""
    # Arrange
    served = importlib.import_module(module)

    # Act / Assert
    with pytest.raises(AttributeError):
        served.__getattr__("NoSuchName")
    with pytest.raises(AttributeError):
        served.__getattr__("_private")
