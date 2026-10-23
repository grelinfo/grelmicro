"""Providers: vendor-specific connection objects shared across components.

A `Provider` owns a native client (Redis pool, asyncpg pool, ...) and the
URL/credentials that built it. Components (`Coordination`, `Cache`, rate limiters,
...) accept a `Provider` instead of opening their own pools, so two
components against the same vendor share one connection.

Read more in the [Providers](../providers/index.md) docs.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.providers._base import Provider

__all__ = [
    "Provider",
]

_LAZY: dict[str, str] = {
    "Provider": "grelmicro.providers._base",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
