"""Regression tests: auto-registered backends unregister on shutdown.

A backend that registered itself as the process default during
construction must clear the registry slot on ``__aexit__``, but only
when the slot still points to the same backend (so a newer backend
that replaced it is left alone).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from grelmicro.cache._backends import cache_backend_registry
from grelmicro.cache.redis import RedisCacheBackend
from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience.memory import MemoryRateLimiterBackend
from grelmicro.resilience.redis import RedisRateLimiterBackend
from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.postgres import PostgresSyncBackend
from grelmicro.sync.redis import RedisSyncBackend
from grelmicro.sync.sqlite import SQLiteSyncBackend

pytestmark = [pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio for async lifecycle tests."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_sync_registry() -> None:
    """Reset the sync backend registry between tests."""
    sync_backend_registry.reset()


@pytest.fixture(autouse=True)
def _clean_rate_limiter_registry() -> None:
    """Reset the rate limiter backend registry between tests."""
    rate_limiter_backend_registry.reset()


@pytest.fixture(autouse=True)
def _clean_cache_registry() -> None:
    """Reset the cache backend registry between tests."""
    cache_backend_registry.reset()


@pytest.fixture
def mock_redis(mocker: MockerFixture) -> MagicMock:
    """Mock the Redis client returned by ``_create_redis_client``."""
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    mock_client.register_script = MagicMock(return_value=AsyncMock())
    # Patch at every call site so all backends share the same mock.
    mocker.patch(
        "grelmicro.sync.redis._create_redis_client",
        return_value=("redis://localhost", mock_client),
    )
    mocker.patch(
        "grelmicro.cache.redis._create_redis_client",
        return_value=("redis://localhost", mock_client),
    )
    mocker.patch(
        "grelmicro.resilience.redis._create_redis_client",
        return_value=("redis://localhost", mock_client),
    )
    return mock_client


# --- Sync (Memory) ---


async def test_sync_memory_backend_unregisters_on_exit() -> None:
    """``async with MemorySyncBackend()`` clears the registry on exit."""
    async with MemorySyncBackend():
        assert sync_backend_registry.is_loaded is True

    assert sync_backend_registry.is_loaded is False


async def test_sync_memory_backend_with_auto_register_false_does_not_touch_registry() -> (
    None
):
    """A backend with ``auto_register=False`` never sets or clears the slot."""
    async with MemorySyncBackend(auto_register=False):
        assert sync_backend_registry.is_loaded is False
    assert sync_backend_registry.is_loaded is False


async def test_sync_memory_backend_does_not_evict_replacement() -> None:
    """Exiting the first backend leaves a newer registered backend in place."""
    backend_1 = MemorySyncBackend()
    assert sync_backend_registry.get() is backend_1

    backend_2 = MemorySyncBackend()  # replaces backend_1 in the slot
    assert sync_backend_registry.get() is backend_2

    # Exiting backend_1 must not evict backend_2 from the registry.
    await backend_1.__aexit__(None, None, None)
    assert sync_backend_registry.get() is backend_2


# --- Rate Limiter (Memory) ---


async def test_rate_limiter_memory_backend_unregisters_on_exit() -> None:
    """``async with MemoryRateLimiterBackend()`` clears the registry on exit."""
    async with MemoryRateLimiterBackend():
        assert rate_limiter_backend_registry.is_loaded is True

    assert rate_limiter_backend_registry.is_loaded is False


async def test_rate_limiter_memory_backend_with_auto_register_false() -> None:
    """A rate limiter backend with ``auto_register=False`` is registry-neutral."""
    async with MemoryRateLimiterBackend(auto_register=False):
        assert rate_limiter_backend_registry.is_loaded is False
    assert rate_limiter_backend_registry.is_loaded is False


async def test_rate_limiter_memory_backend_does_not_evict_replacement() -> None:
    """Exiting the first rate limiter backend leaves a newer one in place."""
    backend_1 = MemoryRateLimiterBackend()
    assert rate_limiter_backend_registry.get() is backend_1

    backend_2 = MemoryRateLimiterBackend()
    assert rate_limiter_backend_registry.get() is backend_2

    await backend_1.__aexit__(None, None, None)
    assert rate_limiter_backend_registry.get() is backend_2


# --- Sync (SQLite) ---


async def test_sync_sqlite_backend_unregisters_on_exit(tmp_path) -> None:  # noqa: ANN001
    """``async with SQLiteSyncBackend()`` clears the registry on exit."""
    db_path = tmp_path / "lock.db"
    async with SQLiteSyncBackend(db_path):
        assert sync_backend_registry.is_loaded is True
    assert sync_backend_registry.is_loaded is False


# --- Sync (Redis) ---


async def test_sync_redis_backend_unregisters_on_exit(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """``async with RedisSyncBackend()`` clears the registry on exit."""
    async with RedisSyncBackend("redis://localhost"):
        assert sync_backend_registry.is_loaded is True
    assert sync_backend_registry.is_loaded is False


# --- Sync (PostgreSQL) ---


async def test_sync_postgres_backend_unregisters_on_exit(
    mocker: MockerFixture,
) -> None:
    """``async with PostgresSyncBackend()`` clears the registry on exit."""
    mock_pool = MagicMock()
    mock_pool.execute = AsyncMock()
    mock_pool.close = AsyncMock()
    mocker.patch(
        "grelmicro.sync.postgres.create_pool",
        AsyncMock(return_value=mock_pool),
    )

    async with PostgresSyncBackend("postgresql://localhost/db"):
        assert sync_backend_registry.is_loaded is True
    assert sync_backend_registry.is_loaded is False


# --- Rate Limiter (Redis) ---


async def test_rate_limiter_redis_backend_unregisters_on_exit(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """``async with RedisRateLimiterBackend()`` clears the registry on exit."""
    async with RedisRateLimiterBackend("redis://localhost"):
        assert rate_limiter_backend_registry.is_loaded is True
    assert rate_limiter_backend_registry.is_loaded is False


# --- Cache (Redis) ---


async def test_cache_redis_backend_unregisters_on_exit(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """``async with RedisCacheBackend()`` clears the registry on exit."""
    async with RedisCacheBackend("redis://localhost"):
        assert cache_backend_registry.is_loaded is True
    assert cache_backend_registry.is_loaded is False
