"""Cache."""

from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
from grelmicro._deprecation import warn_legacy
from grelmicro.cache._backends import cache_backend_registry
from grelmicro.cache._component import Cache
from grelmicro.cache._protocol import CacheBackend
from grelmicro.cache.cached import cached
from grelmicro.cache.errors import CacheError, CacheSettingsValidationError
from grelmicro.cache.serializers import (
    CacheSerializer,
    JsonSerializer,
    PickleSerializer,
    PydanticSerializer,
)
from grelmicro.cache.ttl import CacheInfo, TTLCache


def register(
    backend: Annotated[CacheBackend, Doc("The cache backend.")],
    name: Annotated[
        str, Doc("Name to register the backend under.")
    ] = DEFAULT_NAME,
) -> None:
    """Register ``backend`` under ``name`` (defaults to ``"default"``).

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `Grelmicro(uses=[Cache(backend, name=name)])` instead.
    """
    warn_legacy(
        "grelmicro.cache.register",
        "`Grelmicro(uses=[Cache(backend, name=name)])`",
    )
    cache_backend_registry.register(backend, name)


def unregister(
    name: Annotated[
        str, Doc("Name of the registered backend to remove.")
    ] = DEFAULT_NAME,
    backend: Annotated[
        CacheBackend | None,
        Doc("Optional backend instance for an identity-checked removal."),
    ] = None,
) -> None:
    """Remove the registered backend under ``name``.

    Deprecated since 0.23.0, removed in 1.0.0. Construct a fresh `Grelmicro`
    app instead of mutating a shared registry.
    """
    warn_legacy(
        "grelmicro.cache.unregister",
        "a fresh `Grelmicro(uses=[...])`",
    )
    cache_backend_registry.unregister(name, backend)


def use_backend(
    backend: Annotated[
        CacheBackend,
        Doc("The cache backend to register as the default."),
    ],
) -> None:
    """Register ``backend`` under the ``"default"`` name.

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `Grelmicro(uses=[Cache(backend)])`.
    """
    warn_legacy(
        "grelmicro.cache.use_backend",
        "`Grelmicro(uses=[Cache(backend)])`",
    )
    cache_backend_registry.register(backend, DEFAULT_NAME)


def use(
    backend: Annotated[
        CacheBackend | None,
        Doc('Override the ``"default"`` slot for the duration of the block.'),
    ] = None,
    /,
    **named: CacheBackend,
) -> AbstractContextManager[None]:
    """Install task-scoped backend overrides.

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `async with micro.override(Cache(backend)):` instead.
    """
    warn_legacy(
        "grelmicro.cache.use",
        "`async with micro.override(Cache(backend)):`",
    )
    return cache_backend_registry.use(backend, **named)


__all__ = [
    "Cache",
    "CacheBackend",
    "CacheError",
    "CacheInfo",
    "CacheSerializer",
    "CacheSettingsValidationError",
    "JsonSerializer",
    "PickleSerializer",
    "PydanticSerializer",
    "TTLCache",
    "cached",
    "register",
    "unregister",
    "use",
    "use_backend",
]
