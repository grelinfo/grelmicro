"""Integration Tests for RedisCache Backend."""

import json
from collections.abc import AsyncGenerator, Generator

import pytest
from anyio import sleep
from testcontainers.redis import RedisContainer

from grelmicro.cache.cached import cached
from grelmicro.cache.redis import RedisCache

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.integration,
    pytest.mark.timeout(30),
]

EXPECTED_HITS_1 = 1
EXPECTED_MISSES_1 = 1
EXPECTED_MISSES_2 = 2


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """AnyIO Backend Module Scope."""
    return "asyncio"


@pytest.fixture(scope="module")
def container() -> Generator[RedisContainer, None, None]:
    """Start a Redis testcontainer for the module."""
    with RedisContainer() as redis_container:
        yield redis_container


@pytest.fixture(scope="module")
async def cache(container: RedisContainer) -> AsyncGenerator[RedisCache, None]:
    """Create a module-scoped RedisCache connected to the testcontainer."""
    port = container.get_exposed_port(6379)
    async with RedisCache(
        url=f"redis://localhost:{port}/0", prefix="test:", ttl=60
    ) as redis_cache:
        yield redis_cache


async def test_get_set_roundtrip(cache: RedisCache) -> None:
    """Test that bytes written with set are returned unchanged by get."""
    # Arrange
    key = "roundtrip"
    value = b"hello world"

    # Act
    await cache.set(key, value)
    result = await cache.get(key)

    # Assert
    assert result == value


async def test_get_miss_returns_default(cache: RedisCache) -> None:
    """Test that a nonexistent key returns the specified default value."""
    # Arrange: use a key that was never written
    key = "key_that_does_not_exist_xyz"

    # Act
    result = await cache.get(key, default=b"fallback")

    # Assert
    assert result == b"fallback"


async def test_get_miss_returns_none_when_no_default(cache: RedisCache) -> None:
    """Test that a missing key returns None when no default is provided."""
    # Arrange
    key = "another_missing_key_abc"

    # Act
    result = await cache.get(key)

    # Assert
    assert result is None


async def test_ttl_expiry() -> None:
    """Test that a key becomes unavailable after the TTL elapses."""
    # Arrange: create a separate short-lived cache instance (not module-scoped)
    # This test creates its own container to avoid disturbing the shared fixture.
    # We reuse the module container by obtaining a fresh cache with ttl=1.
    # Because the container fixture is module-scoped, access it via a new fixture
    # param is not possible here, so we start a dedicated container for this test.
    with RedisContainer() as ttl_container:
        port = ttl_container.get_exposed_port(6379)
        url = f"redis://localhost:{port}/0"
        async with RedisCache(url=url, prefix="ttl:", ttl=1) as short_cache:
            key = "expiring_key"
            await short_cache.set(key, b"soon gone")

            # Verify the key is present immediately
            result_before = await short_cache.get(key)
            assert result_before == b"soon gone"

            # Act: wait longer than the TTL
            await sleep(1.5)

            # Assert: the key has expired and the miss default is returned
            result_after = await short_cache.get(key)
            assert result_after is None


async def test_clear_with_prefix_isolation() -> None:
    """Test that clearing one prefixed cache does not affect a differently prefixed one."""
    # Arrange: two caches with different prefixes sharing the same Redis instance
    with RedisContainer() as isolation_container:
        port = isolation_container.get_exposed_port(6379)
        url = f"redis://localhost:{port}/0"
        async with (
            RedisCache(url=url, prefix="alpha:", ttl=60) as cache_alpha,
            RedisCache(url=url, prefix="beta:", ttl=60) as cache_beta,
        ):
            await cache_alpha.set("key1", b"alpha_value")
            await cache_beta.set("key1", b"beta_value")

            # Act: clear only the alpha cache
            await cache_alpha.clear()

            # Assert: alpha key is gone, beta key is unaffected
            alpha_result = await cache_alpha.get("key1")
            beta_result = await cache_beta.get("key1")

            assert alpha_result is None
            assert beta_result == b"beta_value"


async def test_cache_info_counters(cache: RedisCache) -> None:
    """Test that cache_info() counters increment correctly on hits and misses."""
    # Arrange: record baseline counters before this test
    info_before = cache.cache_info()
    hits_before = info_before.hits
    misses_before = info_before.misses

    key = "counter_test_key"
    await cache.set(key, b"counter_value")

    # Act: one hit, one miss
    await cache.get(key)  # hit
    await cache.get("counter_test_missing_key")  # miss

    info_after = cache.cache_info()

    # Assert: exactly one additional hit and one additional miss
    assert info_after.hits == hits_before + 1
    assert info_after.misses == misses_before + 1


async def test_delete(cache: RedisCache) -> None:
    """Test that delete removes the key so subsequent gets return the default."""
    # Arrange
    key = "delete_me"
    await cache.set(key, b"temporary")
    result_before = await cache.get(key)
    assert result_before == b"temporary"

    # Act
    await cache.delete(key)

    # Assert
    result_after = await cache.get(key)
    assert result_after is None


async def test_cached_end_to_end() -> None:
    """Test @cached decorator with RedisCache using JSON serialization round-trip."""
    # Arrange: dedicated Redis instance to keep this test fully isolated
    with RedisContainer() as e2e_container:
        port = e2e_container.get_exposed_port(6379)
        url = f"redis://localhost:{port}/0"
        async with RedisCache(url=url, prefix="e2e:", ttl=60) as e2e_cache:
            call_count = 0

            @cached(
                e2e_cache,
                serializer=lambda v: json.dumps(v).encode(),
                deserializer=json.loads,
            )
            async def fetch_user(user_id: int) -> dict:
                nonlocal call_count
                call_count += 1
                return {"id": user_id, "name": f"user_{user_id}"}

            # Act: first call computes, second call should be served from cache
            first = await fetch_user(42)
            second = await fetch_user(42)

            # Assert: function was only invoked once despite two calls
            assert first == {"id": 42, "name": "user_42"}
            assert second == {"id": 42, "name": "user_42"}
            assert call_count == 1

            # Act: different argument produces a new cache entry
            third = await fetch_user(99)

            # Assert: function invoked a second time for the new key
            assert third == {"id": 99, "name": "user_99"}
            expected_calls = 2
            assert call_count == expected_calls
