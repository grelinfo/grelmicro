"""Cache."""

from grelmicro.cache._protocol import CacheBackend
from grelmicro.cache.cached import cached
from grelmicro.cache.errors import CacheError, CacheSettingsValidationError
from grelmicro.cache.memory import MemoryCacheBackend
from grelmicro.cache.redis import RedisCacheBackend
from grelmicro.cache.ttl import CacheInfo, TTLCache

__all__ = [
    "CacheBackend",
    "CacheError",
    "CacheInfo",
    "CacheSettingsValidationError",
    "MemoryCacheBackend",
    "RedisCacheBackend",
    "TTLCache",
    "cached",
]
