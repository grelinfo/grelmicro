"""Cache."""

from grelmicro.cache._protocol import CacheBackend
from grelmicro.cache.cached import cached
from grelmicro.cache.errors import CacheError, CacheSettingsValidationError
from grelmicro.cache.serializers import (
    JsonSerializer,
    PickleSerializer,
    PydanticSerializer,
)
from grelmicro.cache.ttl import CacheInfo, TTLCache

__all__ = [
    "CacheBackend",
    "CacheError",
    "CacheInfo",
    "CacheSettingsValidationError",
    "JsonSerializer",
    "PickleSerializer",
    "PydanticSerializer",
    "TTLCache",
    "cached",
]
