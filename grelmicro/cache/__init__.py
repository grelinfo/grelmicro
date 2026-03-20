"""Cache Module."""

from grelmicro.cache.cached import cached
from grelmicro.cache.ttl import TTLCache

__all__ = ["TTLCache", "cached"]
