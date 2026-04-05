"""Tests for Cache Backends (parametrized across all implementations)."""

from collections.abc import AsyncGenerator, Generator

import pytest
from anyio import sleep
from testcontainers.redis import RedisContainer

from grelmicro.cache._protocol import CacheBackend
from grelmicro.cache.cached import cached
from grelmicro.cache.memory import MemoryCacheBackend
from grelmicro.cache.redis import RedisCacheBackend
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.cache.ttl import TTLCache

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(30)]


# --- Fixtures (parametrized across backends) ---


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """AnyIO Backend Module Scope."""
    return "asyncio"


@pytest.fixture(
    params=[
        "memory",
        pytest.param("redis", marks=[pytest.mark.integration]),
    ],
    scope="module",
)
def backend_name(request: pytest.FixtureRequest) -> str:
    """Backend name."""
    return request.param


@pytest.fixture(scope="module")
def container(
    backend_name: str,
) -> Generator[RedisContainer | None, None, None]:
    """Docker container (only for Redis)."""
    if backend_name == "redis":
        with RedisContainer() as redis_container:
            yield redis_container
    else:
        yield None


@pytest.fixture(scope="module")
async def backend(
    backend_name: str, container: RedisContainer | None
) -> AsyncGenerator[CacheBackend]:
    """Cache backend instance."""
    if backend_name == "redis" and container:
        port = container.get_exposed_port(6379)
        async with RedisCacheBackend(
            f"redis://localhost:{port}/0",
            prefix="test:",
            auto_register=False,
        ) as redis_backend:
            yield redis_backend
    elif backend_name == "memory":
        async with MemoryCacheBackend(auto_register=False) as memory_backend:
            yield memory_backend


# --- Shared tests (run against all backends) ---


async def test_get_set_roundtrip(backend: CacheBackend) -> None:
    """Test that bytes written with set are returned unchanged by get."""
    await backend.set(key="roundtrip", value=b"hello", ttl=60)

    result = await backend.get(key="roundtrip")

    assert result == b"hello"


async def test_get_miss_returns_none(backend: CacheBackend) -> None:
    """Test that a nonexistent key returns None."""
    result = await backend.get(key="nonexistent_key_xyz")

    assert result is None


async def test_ttl_expiry(backend: CacheBackend) -> None:
    """Test that a key becomes unavailable after the TTL elapses."""
    await backend.set(key="expiring", value=b"temp", ttl=0.5)

    assert await backend.get(key="expiring") == b"temp"

    await sleep(1.0)

    assert await backend.get(key="expiring") is None


async def test_delete(backend: CacheBackend) -> None:
    """Test that delete removes a key."""
    await backend.set(key="to_delete", value=b"bye", ttl=60)
    assert await backend.get(key="to_delete") == b"bye"

    await backend.delete(key="to_delete")

    assert await backend.get(key="to_delete") is None


async def test_delete_missing_key_is_no_op(backend: CacheBackend) -> None:
    """Test that deleting an absent key does not raise."""
    await backend.delete(key="never_existed_abc")


async def test_clear(backend: CacheBackend) -> None:
    """Test that clear removes all entries."""
    await backend.set(key="clear_a", value=b"a", ttl=60)
    await backend.set(key="clear_b", value=b"b", ttl=60)

    await backend.clear()

    assert await backend.get(key="clear_a") is None
    assert await backend.get(key="clear_b") is None


async def test_overwrite(backend: CacheBackend) -> None:
    """Test that setting the same key overwrites the previous value."""
    await backend.set(key="overwrite", value=b"old", ttl=60)
    await backend.set(key="overwrite", value=b"new", ttl=60)

    result = await backend.get(key="overwrite")

    assert result == b"new"


# --- Redis-specific tests ---


@pytest.mark.integration
async def test_redis_prefix_isolation() -> None:
    """Test that clearing one prefix does not affect another."""
    with RedisContainer() as container:
        port = container.get_exposed_port(6379)
        url = f"redis://localhost:{port}/0"
        async with (
            RedisCacheBackend(
                url, prefix="alpha:", auto_register=False
            ) as alpha,
            RedisCacheBackend(url, prefix="beta:", auto_register=False) as beta,
        ):
            await alpha.set(key="k", value=b"alpha", ttl=60)
            await beta.set(key="k", value=b"beta", ttl=60)

            await alpha.clear()

            assert await alpha.get(key="k") is None
            assert await beta.get(key="k") == b"beta"


# --- End-to-end: TTLCache + @cached ---


@pytest.mark.integration
async def test_cached_end_to_end_with_redis() -> None:
    """Test @cached with TTLCache backed by Redis."""
    with RedisContainer() as container:
        port = container.get_exposed_port(6379)
        url = f"redis://localhost:{port}/0"
        async with RedisCacheBackend(
            url, prefix="e2e:", auto_register=False
        ) as redis_backend:
            cache = TTLCache(
                ttl=60,
                backend=redis_backend,
                serializer=JsonSerializer(),
            )
            call_count = 0

            @cached(cache)
            async def fetch_user(user_id: int) -> dict:
                nonlocal call_count
                call_count += 1
                return {"id": user_id}

            first = await fetch_user(1)
            second = await fetch_user(1)

            assert first == {"id": 1}
            assert second == {"id": 1}
            assert call_count == 1


async def test_cached_end_to_end_with_memory() -> None:
    """Test @cached with TTLCache backed by MemoryCacheBackend."""
    async with MemoryCacheBackend(auto_register=False) as memory_backend:
        cache = TTLCache(
            ttl=60,
            backend=memory_backend,
            serializer=JsonSerializer(),
        )
        call_count = 0

        @cached(cache)
        async def fetch_user(user_id: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        first = await fetch_user(1)
        second = await fetch_user(1)

        assert first == {"id": 1}
        assert second == {"id": 1}
        assert call_count == 1
