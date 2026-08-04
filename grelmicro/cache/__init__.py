"""Cache."""

from grelmicro.cache._component import Cache
from grelmicro.cache._protocol import CacheBackend
from grelmicro.cache.cached import CachedFunction, CachedStream, cached
from grelmicro.cache.errors import CacheError, CacheSettingsValidationError
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
    "CacheError",
    "CacheInfo",
    "CacheSerializer",
    "CacheSettingsValidationError",
    "CachedFunction",
    "CachedStream",
    "JsonSerializer",
    "PickleSerializer",
    "PydanticSerializer",
    "TTLCache",
    "TTLCacheConfig",
    "cached",
]
