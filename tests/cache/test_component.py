"""Tests for the `Cache` component (Grelmicro app integration)."""

from __future__ import annotations

import pytest

from grelmicro import Component, Grelmicro
from grelmicro.cache import Cache, JsonSerializer, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider


def test_cache_satisfies_component_protocol() -> None:
    """`Cache` is a runtime-checkable `Component`."""
    assert isinstance(Cache(MemoryCacheAdapter()), Component)


def test_cache_default_kind_and_name() -> None:
    """Default kind is `cache` and default name is `default`."""
    cache = Cache(MemoryCacheAdapter())
    assert cache.kind == "cache"
    assert cache.name == "default"


def test_cache_named_registration() -> None:
    """A named `Cache` component coexists with the default one."""
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            Cache(MemoryCacheAdapter(), name="responses"),
        ]
    )
    assert micro.get("cache", "default").name == "default"
    assert micro.get("cache", "responses").name == "responses"


def test_cache_backend_property() -> None:
    """`cache.backend` returns the wrapped backend."""
    backend = MemoryCacheAdapter()
    cache = Cache(backend)
    assert cache.backend is backend


async def test_cache_ttl_factory_binds_backend() -> None:
    """`cache.ttl(...)` creates a `TTLCache` whose writes round-trip via the same backend."""
    backend = MemoryCacheAdapter()
    cache_a = Cache(backend)
    cache_b = Cache(backend)  # second wrapper over the SAME backend
    ttl_a = cache_a.ttl(ttl=300)
    ttl_b = cache_b.ttl(ttl=300)
    assert isinstance(ttl_a, TTLCache)
    async with backend:
        await ttl_a.set("k", b"v")
        # Same backend ⇒ second TTLCache reads what the first wrote.
        assert await ttl_b.get("k") == b"v"


async def test_cache_ttl_factory_passes_serializer() -> None:
    """`cache.ttl(serializer=...)` round-trips a non-bytes value through the serializer."""
    cache = Cache(MemoryCacheAdapter())
    ttl_cache = cache.ttl(ttl=60, serializer=JsonSerializer())
    async with cache:
        await ttl_cache.set("payload", {"id": 1, "tags": ["a", "b"]})
        assert await ttl_cache.get("payload") == {"id": 1, "tags": ["a", "b"]}


async def test_cache_cached_decorator_works_via_micro_attribute() -> None:
    """`@micro.cache.cached(ttl_cache)` decorates and caches results."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    calls = 0
    async with micro:
        ttl_cache = micro.cache.ttl(ttl=60, serializer=JsonSerializer())

        @micro.cache.cached(ttl_cache)
        async def lookup(user_id: int) -> dict:
            nonlocal calls
            calls += 1
            return {"id": user_id, "name": "alice"}

        first = await lookup(1)
        second = await lookup(1)
        assert first == second == {"id": 1, "name": "alice"}
        # Second call hit the cache, not the function.
        assert calls == 1


async def test_cache_opens_and_closes_backend_with_app() -> None:
    """`async with micro:` opens and closes the underlying backend."""
    backend = MemoryCacheAdapter()
    cache = Cache(backend)
    micro = Grelmicro(uses=[cache])
    async with micro:
        ttl_cache = cache.ttl(ttl=60)
        await ttl_cache.set("k", b"v")
        assert await ttl_cache.get("k") == b"v"


async def test_micro_cache_via_attribute() -> None:
    """`micro.cache.ttl(...)` is the conventional access path."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    async with micro:
        ttl_cache = micro.cache.ttl(ttl=60, serializer=JsonSerializer())
        await ttl_cache.set("alice", {"id": 1})
        assert await ttl_cache.get("alice") == {"id": 1}


async def test_use_auto_wraps_raw_cache_backend() -> None:
    """`micro.use(MemoryCacheAdapter())` auto-wraps the backend in `Cache`."""
    backend = MemoryCacheAdapter()
    micro = Grelmicro(uses=[backend])
    assert isinstance(micro.cache, Cache)
    assert micro.cache.backend is backend


async def test_use_auto_wrap_cache_lifecycles_backend() -> None:
    """Auto-wrapped cache backend opens and closes with the app."""
    backend = MemoryCacheAdapter()
    micro = Grelmicro(uses=[backend])
    async with micro:
        ttl_cache = micro.cache.ttl(ttl=60)
        await ttl_cache.set("k", b"v")
        assert await ttl_cache.get("k") == b"v"


async def test_micro_cache_prefers_default_over_named() -> None:
    """`micro.cache` resolves to `(cache, default)` even when named components exist."""
    primary = MemoryCacheAdapter()
    micro = Grelmicro(
        uses=[
            Cache(primary),
            Cache(MemoryCacheAdapter(), name="responses"),
        ]
    )
    assert micro.cache.backend is primary


async def test_micro_cache_raises_when_ambiguous() -> None:
    """`micro.cache` raises when multiple non-default components exist."""
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter(), name="a"),
            Cache(MemoryCacheAdapter(), name="b"),
        ]
    )
    with pytest.raises(AttributeError, match="multiple 'cache' components"):
        _ = micro.cache


def test_cache_accepts_redis_provider() -> None:
    """`Cache(RedisProvider(...))` calls `provider.cache()` to build the adapter."""
    provider = RedisProvider("redis://localhost:6379/0")
    cache = Cache(provider)
    assert isinstance(cache.backend, RedisCacheAdapter)
    assert cache.backend.provider is provider


def test_cache_with_postgres_provider_raises() -> None:
    """`Cache(PostgresProvider(...))` raises `NotImplementedError` (no cache adapter)."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    with pytest.raises(NotImplementedError, match="no cache adapter"):
        Cache(provider)
