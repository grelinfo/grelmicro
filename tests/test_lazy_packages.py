"""Packages with lazy re-exports resolve every public name on first access."""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

import pytest

_LAZY_PACKAGES = ["grelmicro.health", "grelmicro.metrics", "grelmicro.trace"]
"""The packages whose `__init__` loads each public name on first access."""


def _package(name: str) -> ModuleType:
    return importlib.import_module(name)


def _submodules(package: ModuleType) -> set[str]:
    return {info.name for info in pkgutil.iter_modules(package.__path__)}


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_every_public_name_resolves_to_its_submodule_object(name: str) -> None:
    """Each name in `__all__` is the object its submodule defines."""
    # Arrange
    package = _package(name)

    # Act
    resolved = {attr: getattr(package, attr) for attr in package.__all__}

    # Assert
    for attr, value in resolved.items():
        source = importlib.import_module(package._LAZY[attr])
        assert value is getattr(source, attr)


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_lazy_table_covers_exactly_the_public_names(name: str) -> None:
    """The name-to-submodule table lists every name in `__all__` and no other."""
    # Arrange
    package = _package(name)

    # Act
    table = set(package._LAZY)

    # Assert
    assert table == set(package.__all__)


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_dir_lists_the_public_names(name: str) -> None:
    """`dir()` shows `__all__`, the loaded submodules and private names only."""
    # Arrange
    package = _package(name)

    # Act
    public = {attr for attr in dir(package) if not attr.startswith("_")}

    # Assert
    assert public - _submodules(package) == set(package.__all__)


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_star_import_binds_every_public_name(name: str) -> None:
    """`from package import *` binds each name in `__all__`."""
    # Arrange
    namespace: dict[str, object] = {}

    # Act
    exec(f"from {name} import *", namespace)  # noqa: S102

    # Assert
    package = _package(name)
    for attr in package.__all__:
        assert namespace[attr] is getattr(package, attr)


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_unknown_name_raises_attribute_error(name: str) -> None:
    """A name outside `__all__` raises `AttributeError` naming the package."""
    # Arrange
    package = _package(name)

    # Act / Assert
    with pytest.raises(AttributeError, match=f"module '{name}' has no attr"):
        package.missing_name  # noqa: B018


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_resolved_name_is_cached_on_the_package(name: str) -> None:
    """The first access stores the name in the package namespace."""
    # Arrange
    package = _package(name)
    attr = package.__all__[0]
    vars(package).pop(attr, None)

    # Act
    value = getattr(package, attr)

    # Assert
    assert vars(package)[attr] is value
