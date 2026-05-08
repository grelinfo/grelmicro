"""Tests for the `Cache` module (Grelmicro app integration)."""

from __future__ import annotations

import pytest

from grelmicro import Grelmicro, Module
from grelmicro.cache import Cache, JsonSerializer, TTLCache
from grelmicro.cache.memory import MemoryCacheBackend


def test_cache_satisfies_module_protocol() -> None:
    """`Cache` is a runtime-checkable `Module`."""
    assert isinstance(Cache(MemoryCacheBackend()), Module)


def test_cache_default_kind_and_name() -> None:
    """Default kind is `cache` and default name is `default`."""
    cache = Cache(MemoryCacheBackend())
    assert cache.kind == "cache"
    assert cache.name == "default"


def test_cache_named_registration() -> None:
    """A named `Cache` module coexists with the default one."""
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheBackend()),
            Cache(MemoryCacheBackend(), name="responses"),
        ]
    )
    assert micro.get("cache", "default").name == "default"
    assert micro.get("cache", "responses").name == "responses"


def test_cache_backend_property() -> None:
    """`cache.backend` returns the wrapped backend."""
    backend = MemoryCacheBackend()
    cache = Cache(backend)
    assert cache.backend is backend


async def test_cache_ttl_factory_binds_backend() -> None:
    """`cache.ttl(...)` creates a `TTLCache` whose writes round-trip via the same backend."""
    backend = MemoryCacheBackend()
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
    cache = Cache(MemoryCacheBackend())
    ttl_cache = cache.ttl(ttl=60, serializer=JsonSerializer())
    async with cache:
        await ttl_cache.set("payload", {"id": 1, "tags": ["a", "b"]})
        assert await ttl_cache.get("payload") == {"id": 1, "tags": ["a", "b"]}


async def test_cache_cached_decorator_works_via_micro_attribute() -> None:
    """`@micro.cache.cached(ttl_cache)` decorates and caches results."""
    micro = Grelmicro(uses=[Cache(MemoryCacheBackend())])
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
    backend = MemoryCacheBackend()
    cache = Cache(backend)
    micro = Grelmicro(uses=[cache])
    async with micro:
        ttl_cache = cache.ttl(ttl=60)
        await ttl_cache.set("k", b"v")
        assert await ttl_cache.get("k") == b"v"


async def test_micro_cache_via_attribute() -> None:
    """`micro.cache.ttl(...)` is the conventional access path."""
    micro = Grelmicro(uses=[Cache(MemoryCacheBackend())])
    async with micro:
        ttl_cache = micro.cache.ttl(ttl=60, serializer=JsonSerializer())
        await ttl_cache.set("alice", {"id": 1})
        assert await ttl_cache.get("alice") == {"id": 1}


async def test_use_auto_wraps_raw_cache_backend() -> None:
    """`micro.use(MemoryCacheBackend())` auto-wraps the backend in `Cache`."""
    backend = MemoryCacheBackend()
    micro = Grelmicro(uses=[backend])
    assert isinstance(micro.cache, Cache)
    assert micro.cache.backend is backend


async def test_use_auto_wrap_cache_lifecycles_backend() -> None:
    """Auto-wrapped cache backend opens and closes with the app."""
    backend = MemoryCacheBackend()
    micro = Grelmicro(uses=[backend])
    async with micro:
        ttl_cache = micro.cache.ttl(ttl=60)
        await ttl_cache.set("k", b"v")
        assert await ttl_cache.get("k") == b"v"


async def test_micro_cache_prefers_default_over_named() -> None:
    """`micro.cache` resolves to `(cache, default)` even when named modules exist."""
    primary = MemoryCacheBackend()
    micro = Grelmicro(
        uses=[
            Cache(primary),
            Cache(MemoryCacheBackend(), name="responses"),
        ]
    )
    assert micro.cache.backend is primary


async def test_micro_cache_raises_when_ambiguous() -> None:
    """`micro.cache` raises when multiple non-default modules exist."""
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheBackend(), name="a"),
            Cache(MemoryCacheBackend(), name="b"),
        ]
    )
    with pytest.raises(AttributeError, match="multiple 'cache' modules"):
        _ = micro.cache
