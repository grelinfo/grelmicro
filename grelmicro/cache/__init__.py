"""Cache."""

from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
from grelmicro.cache._backends import cache_backend_registry
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
    """Register ``backend`` under ``name`` (defaults to ``"default"``)."""
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
    """Remove the registered backend under ``name``."""
    cache_backend_registry.unregister(name, backend)


def use_backend(
    backend: Annotated[
        CacheBackend,
        Doc("The cache backend to register as the default."),
    ],
) -> None:
    """Register ``backend`` under the ``"default"`` name."""
    cache_backend_registry.register(backend, DEFAULT_NAME)


def use(
    backend: Annotated[
        CacheBackend | None,
        Doc('Override the ``"default"`` slot for the duration of the block.'),
    ] = None,
    /,
    **named: CacheBackend,
) -> AbstractContextManager[None]:
    """Install task-scoped backend overrides."""
    return cache_backend_registry.use(backend, **named)


__all__ = [
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
