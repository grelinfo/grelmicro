"""Cache."""

from typing import Annotated

from typing_extensions import Doc

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


def use_backend(
    backend: Annotated[
        CacheBackend,
        Doc("The cache backend to register as the default."),
    ],
) -> None:
    """Register `backend` as the default cache backend.

    Idempotent: re-registering the same instance is a no-op.
    Registering a different instance warns and replaces.
    """
    cache_backend_registry.register(backend)


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
    "use_backend",
]
