"""Chaos: cache stale-on-error against real Redis.

Docs claim for `@cached(stale_ttl=...)`: when a later recompute (on a
miss after the TTL lapses) raises, the most recent value is served
instead of propagating the error, for up to `stale_ttl` seconds past
the TTL.

Two distinct failure modes, and they are NOT the same thing:

* 4a (the documented promise): Redis is healthy, the recompute function
  raises. The stale reserve is read from Redis and served. PASS path.

* 4b (the unstated edge): the cache backend itself dies. The stale read
  is also a backend read, so it fails too. We observe and report what
  actually happens, since a user might wrongly expect stale-on-error to
  cover a dead cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from grelmicro.cache._key import make_cache_key
from grelmicro.cache.cached import cached
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.providers.redis import RedisProvider

from .conftest import build_client, paused, wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from testcontainers.redis import RedisContainer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(60),
]


@pytest.fixture
async def cache_backend(
    redis_container: RedisContainer,
) -> AsyncGenerator[RedisCacheAdapter]:
    """Yield a Redis cache adapter on a bounded-timeout client with a fresh keyspace."""
    client = build_client(redis_container)
    provider = RedisProvider.from_client(client, own=True)
    async with provider:
        await provider.client.flushdb()
        async with RedisCacheAdapter(provider=provider) as adapter:
            yield adapter


async def test_recompute_failure_after_ttl_serves_stale(
    cache_backend: RedisCacheAdapter,
) -> None:
    """4a: Redis up, recompute raises after TTL, the stale value is served.

    Short TTL, generous stale_ttl. First call seeds the value. After the
    TTL lapses the entry is a miss, the recompute raises, and the stale
    reserve (still alive in Redis) is served instead of the error.
    """
    cache = TTLCache(
        ttl=0.5,
        backend=cache_backend,
        serializer=JsonSerializer(),
    )

    state = {"fail": False, "value": "fresh"}

    @cached(cache, stale_ttl=30)
    async def fetch(_key: str) -> str:
        if state["fail"]:
            msg = "upstream is down"
            raise RuntimeError(msg)
        return cast("str", state["value"])

    key = uuid4().hex
    assert await fetch(key) == "fresh"  # seeds value + stale reserve

    # Wait past the TTL deterministically by polling the live backend
    # value (not a bare sleep). The stale reserve outlives it by stale_ttl.
    cache_key = _cached_key(fetch, key)

    async def value_gone() -> bool:
        raw = await cache_backend.get(key=f"cache:{cache_key}")
        return raw is None

    assert await wait_until(value_gone, timeout=5), "TTL entry must expire"

    # Now recompute fails. Stale reserve is still alive -> served.
    state["fail"] = True
    served = await fetch(key)
    assert served == "fresh", (
        "stale-on-error must serve the last-good value when recompute fails"
    )


async def test_cache_backend_death_during_read_propagates(
    redis_container: RedisContainer,
    cache_backend: RedisCacheAdapter,
) -> None:
    """4b: the cache backend itself dies. Observe and document behavior.

    `stale_ttl` only protects against a failing recompute, not against a
    dead cache. With Redis paused, even the initial `get` (and the stale
    read fallback) is a backend round-trip that times out, so the call
    raises. We assert exactly that and report it as a docs caveat: a dead
    cache backend is NOT covered by stale-on-error.
    """
    cache = TTLCache(
        ttl=30,
        backend=cache_backend,
        serializer=JsonSerializer(),
    )

    @cached(cache, stale_ttl=30)
    async def fetch(_key: str) -> str:
        return "value"

    key = uuid4().hex
    assert await fetch(key) == "value"  # warm the entry while healthy

    with paused(redis_container):
        # The cache read itself now times out. stale_ttl cannot help
        # because the stale read is also a backend call.
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await fetch(uuid4().hex)  # cold key -> forces a backend read
        # Document the surfaced error type for the report.
        assert exc_info.value is not None

    async def healthy_again() -> bool:
        return await fetch(uuid4().hex) == "value"

    assert await wait_until(healthy_again, timeout=15)


def _cached_key(func: object, *args: object) -> str:
    """Recreate the cache key the decorator computes for ``func(*args)``."""
    unwrapped = cast("Any", func).__wrapped__
    return make_cache_key(unwrapped, args, {}, typed=False)
