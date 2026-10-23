"""Cache.

`cached`, `CachedFunction` and `CachedStream` load with the package. Each
other name loads its submodule on first access.
"""

import typing as _typing

# `cached` is also the name of a submodule, and importing a submodule binds
# it on the package, so the function is bound here before any lazy load.
from grelmicro.cache.cached import CachedFunction, CachedStream, cached

if _typing.TYPE_CHECKING:
    from grelmicro.cache._component import Cache
    from grelmicro.cache._protocol import CacheBackend
    from grelmicro.cache.serializers import (
        CacheSerializer,
        JsonSerializer,
        PickleSerializer,
        PydanticSerializer,
    )
    from grelmicro.cache.ttl import CacheInfo, TTLCache, TTLCacheConfig

__all__ = [
    "Cache",
    "CacheBackend",
    "CacheInfo",
    "CacheSerializer",
    "CachedFunction",
    "CachedStream",
    "JsonSerializer",
    "PickleSerializer",
    "PydanticSerializer",
    "TTLCache",
    "TTLCacheConfig",
    "cached",
]

_LAZY: dict[str, str] = {
    "Cache": "grelmicro.cache._component",
    "CacheBackend": "grelmicro.cache._protocol",
    "CacheInfo": "grelmicro.cache.ttl",
    "CacheSerializer": "grelmicro.cache.serializers",
    "JsonSerializer": "grelmicro.cache.serializers",
    "PickleSerializer": "grelmicro.cache.serializers",
    "PydanticSerializer": "grelmicro.cache.serializers",
    "TTLCache": "grelmicro.cache.ttl",
    "TTLCacheConfig": "grelmicro.cache.ttl",
}
"""The submodule each public name not bound at import loads from."""


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
