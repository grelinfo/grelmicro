"""Packages with lazy re-exports resolve every public name on first access."""

from __future__ import annotations

import importlib
import pkgutil
import warnings
from types import ModuleType

import pytest

from grelmicro.errors import GrelmicroFutureWarning

_LAZY_PACKAGES = [
    "grelmicro.cache",
    "grelmicro.clock",
    "grelmicro.config",
    "grelmicro.coordination",
    "grelmicro.health",
    "grelmicro.http",
    "grelmicro.idempotency",
    "grelmicro.metrics",
    "grelmicro.outbox",
    "grelmicro.providers",
    "grelmicro.resilience",
    "grelmicro.resilience.shield",
    "grelmicro.security",
    "grelmicro.task",
    "grelmicro.trace",
]
"""The packages whose `__init__` loads each public name on first access."""

_EAGER: dict[str, dict[str, str]] = {
    "grelmicro.cache": {
        "CachedFunction": "grelmicro.cache.cached",
        "CachedStream": "grelmicro.cache.cached",
        "cached": "grelmicro.cache.cached",
    },
    "grelmicro.resilience": {
        "fallback": "grelmicro.resilience.fallback",
        "falling_back": "grelmicro.resilience.fallback",
        "retry": "grelmicro.resilience.retry",
        "retrying": "grelmicro.resilience.retry",
        "shield": "grelmicro.resilience.shield",
    },
}
"""The public names a package binds at import, and their submodule, by package.

Each of these packages has a public name that is also a submodule name.
"""


def _package(name: str) -> ModuleType:
    return importlib.import_module(name)


def _removed(package: ModuleType) -> frozenset[str]:
    """Return the names in `__all__` removed in a coming release.

    Each one warns on every access, so the package serves it from
    `__getattr__` and never lists it in `_LAZY`.
    """
    return getattr(package, "_MODE_ENUMS", frozenset())


def _submodules(package: ModuleType) -> set[str]:
    return {info.name for info in pkgutil.iter_modules(package.__path__)}


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_every_public_name_resolves_to_its_submodule_object(name: str) -> None:
    """Each name in `__all__` is the object its submodule defines."""
    # Arrange
    package = _package(name)
    sources = {**package._LAZY, **_EAGER.get(name, {})}

    # Act
    resolved = {
        attr: getattr(package, attr)
        for attr in set(package.__all__) - _removed(package)
    }

    # Assert
    for attr, value in resolved.items():
        source = importlib.import_module(sources[attr])
        assert value is getattr(source, attr)


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_lazy_table_covers_exactly_the_public_names(name: str) -> None:
    """The table lists every name in `__all__` bound neither at import nor on each access."""
    # Arrange
    package = _package(name)
    eager = set(_EAGER.get(name, {}))

    # Act
    table = set(package._LAZY)

    # Assert
    assert table == set(package.__all__) - eager - _removed(package)
    assert _removed(package) <= set(package.__all__)
    assert eager <= set(package.__all__)


@pytest.mark.parametrize(
    ("name", "attr"),
    [(name, attr) for name, names in _EAGER.items() for attr in names],
)
def test_name_shared_with_a_submodule_stays_the_function(
    name: str, attr: str
) -> None:
    """A public name that is also a submodule name never resolves to the module."""
    # Arrange
    package = _package(name)
    for submodule in _submodules(package):
        importlib.import_module(f"{name}.{submodule}")

    # Act
    value = getattr(package, attr)

    # Assert
    assert not isinstance(value, ModuleType)
    assert value is getattr(importlib.import_module(_EAGER[name][attr]), attr)


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_dir_lists_the_public_names(name: str) -> None:
    """`dir()` shows `__all__`, the loaded submodules and private names only."""
    # Arrange
    package = _package(name)

    # Act
    public = {attr for attr in dir(package) if not attr.startswith("_")}

    # Assert
    assert public - _submodules(package) == set(package.__all__) - _submodules(
        package
    )
    assert set(package.__all__) <= public


@pytest.mark.parametrize("name", _LAZY_PACKAGES)
def test_star_import_binds_every_public_name(name: str) -> None:
    """`from package import *` binds each name in `__all__`."""
    # Arrange
    namespace: dict[str, object] = {}

    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", GrelmicroFutureWarning)
        exec(f"from {name} import *", namespace)  # noqa: S102

    # Assert
    package = _package(name)
    assert set(package.__all__) <= set(namespace)
    for attr in set(package.__all__) - _removed(package):
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
    attr = next(iter(package._LAZY))
    vars(package).pop(attr, None)

    # Act
    value = getattr(package, attr)

    # Assert
    assert vars(package)[attr] is value
