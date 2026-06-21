"""Boundary tests for TTLCache validation and LRU eviction.

The broader suite checks constructor validation and basic get/set. These pin
the per-call `ttl`/`stale_ttl` validation boundary and the `maxsize > 0` LRU
guard, so a flipped comparison (`<= 0` to `<= 1`, `> 0` to `>= 0`/`> 1`) is
caught.
"""

from __future__ import annotations

import pytest

from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter

_BASE_TTL = 60.0
_FILL = 5


@pytest.fixture
def backend() -> MemoryCacheAdapter:
    """Provide an isolated in-memory cache backend."""
    return MemoryCacheAdapter()


async def test_per_call_ttl_of_one_is_accepted(
    backend: MemoryCacheAdapter,
) -> None:
    """A per-call `ttl=1` is valid (the guard rejects `<= 0`, not `<= 1`)."""
    async with Grelmicro(uses=[Cache(backend)]):
        cache = TTLCache(maxsize=10, ttl=_BASE_TTL, serializer=JsonSerializer())
        await cache.set("k", "v", ttl=1)
        assert await cache.get("k", None) == "v"


async def test_per_call_stale_ttl_of_one_is_accepted(
    backend: MemoryCacheAdapter,
) -> None:
    """A per-call `stale_ttl=1` is valid."""
    async with Grelmicro(uses=[Cache(backend)]):
        cache = TTLCache(maxsize=10, ttl=_BASE_TTL, serializer=JsonSerializer())
        await cache.set("k", "v", ttl=_BASE_TTL, stale_ttl=1)
        assert await cache.get("k", None) == "v"


async def test_maxsize_one_evicts_the_oldest_key(
    backend: MemoryCacheAdapter,
) -> None:
    """With `maxsize=1` a second key evicts the first (LRU is active)."""
    async with Grelmicro(uses=[Cache(backend)]):
        cache = TTLCache(maxsize=1, ttl=_BASE_TTL, serializer=JsonSerializer())
        await cache.set("a", "1")
        await cache.set("b", "2")
        assert await cache.get("a", None) is None
        assert await cache.get("b", None) == "2"


async def test_maxsize_zero_keeps_every_key(
    backend: MemoryCacheAdapter,
) -> None:
    """`maxsize=0` is unlimited: no key is evicted."""
    async with Grelmicro(uses=[Cache(backend)]):
        cache = TTLCache(maxsize=0, ttl=_BASE_TTL, serializer=JsonSerializer())
        for i in range(_FILL):
            await cache.set(f"k{i}", str(i))
        for i in range(_FILL):
            assert await cache.get(f"k{i}", None) == str(i)
