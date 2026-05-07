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
        modules=[
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


def test_cache_ttl_factory_binds_backend() -> None:
    """`cache.ttl(...)` creates a `TTLCache` bound to the wrapped backend."""
    backend = MemoryCacheBackend()
    cache = Cache(backend)
    ttl_cache = cache.ttl(ttl=300)
    assert isinstance(ttl_cache, TTLCache)
    assert ttl_cache._backend is backend


def test_cache_ttl_factory_passes_serializer() -> None:
    """`cache.ttl(serializer=...)` forwards the serializer to `TTLCache`."""
    serializer = JsonSerializer()
    cache = Cache(MemoryCacheBackend())
    ttl_cache = cache.ttl(ttl=60, serializer=serializer)
    assert ttl_cache._serializer is serializer


async def test_cache_opens_and_closes_backend_with_app() -> None:
    """`async with micro:` opens and closes the underlying backend."""
    backend = MemoryCacheBackend()
    cache = Cache(backend)
    micro = Grelmicro(modules=[cache])
    async with micro:
        ttl_cache = cache.ttl(ttl=60)
        await ttl_cache.set("k", b"v")
        assert await ttl_cache.get("k") == b"v"


async def test_micro_cache_via_attribute() -> None:
    """`micro.cache.ttl(...)` is the conventional access path."""
    micro = Grelmicro(modules=[Cache(MemoryCacheBackend())])
    async with micro:
        ttl_cache = micro.cache.ttl(ttl=60, serializer=JsonSerializer())
        await ttl_cache.set("alice", {"id": 1})
        assert await ttl_cache.get("alice") == {"id": 1}


async def test_micro_cache_prefers_default_over_named() -> None:
    """`micro.cache` resolves to `(cache, default)` even when named modules exist."""
    primary = MemoryCacheBackend()
    micro = Grelmicro(
        modules=[
            Cache(primary),
            Cache(MemoryCacheBackend(), name="responses"),
        ]
    )
    assert micro.cache.backend is primary


async def test_micro_cache_raises_when_ambiguous() -> None:
    """`micro.cache` raises when multiple non-default modules exist."""
    micro = Grelmicro(
        modules=[
            Cache(MemoryCacheBackend(), name="a"),
            Cache(MemoryCacheBackend(), name="b"),
        ]
    )
    with pytest.raises(AttributeError, match="multiple 'cache' modules"):
        _ = micro.cache
