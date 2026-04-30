"""Backend lifecycle: explicit registration via ``async with``.

Constructors are pure: they perform no registry writes.
Registration happens on ``__aenter__`` and unregistration on
``__aexit__``. ``unregister`` uses an identity check, so a backend
that was already replaced by a newer instance leaves the slot alone.
"""

import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from grelmicro.cache._backends import cache_backend_registry
from grelmicro.cache.memory import MemoryCacheBackend
from grelmicro.cache.redis import RedisCacheBackend
from grelmicro.resilience import use_backend as resilience_use_backend
from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience.memory import MemoryRateLimiterBackend
from grelmicro.resilience.redis import RedisRateLimiterBackend
from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync.kubernetes import KubernetesSyncBackend
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


# --- Pure constructors ---


def test_sync_memory_constructor_does_not_register() -> None:
    """Constructing a sync backend performs no registry writes."""
    MemorySyncBackend()
    assert sync_backend_registry.is_loaded is False


def test_cache_memory_constructor_does_not_register() -> None:
    """Constructing a cache backend performs no registry writes."""
    MemoryCacheBackend()
    assert cache_backend_registry.is_loaded is False


def test_rate_limiter_memory_constructor_does_not_register() -> None:
    """Constructing a rate limiter backend performs no registry writes."""
    MemoryRateLimiterBackend()
    assert rate_limiter_backend_registry.is_loaded is False


# --- Sync (Memory) ---


async def test_sync_memory_backend_round_trip() -> None:
    """``async with`` registers on enter and unregisters on exit."""
    async with MemorySyncBackend():
        assert sync_backend_registry.is_loaded is True
    assert sync_backend_registry.is_loaded is False


async def test_sync_memory_backend_does_not_evict_replacement() -> None:
    """Exiting an older backend leaves a newer registered one in place."""
    backend_1 = MemorySyncBackend()
    backend_2 = MemorySyncBackend()
    sync_backend_registry.register(backend_1)
    sync_backend_registry.register(backend_2)
    assert sync_backend_registry.get() is backend_2

    await backend_1.__aexit__(None, None, None)
    assert sync_backend_registry.get() is backend_2


# --- Rate Limiter (Memory) ---


async def test_rate_limiter_memory_backend_round_trip() -> None:
    """``async with`` registers on enter and unregisters on exit."""
    async with MemoryRateLimiterBackend():
        assert rate_limiter_backend_registry.is_loaded is True
    assert rate_limiter_backend_registry.is_loaded is False


async def test_rate_limiter_memory_backend_does_not_evict_replacement() -> None:
    """Exiting an older rate limiter backend leaves a newer one in place."""
    backend_1 = MemoryRateLimiterBackend()
    backend_2 = MemoryRateLimiterBackend()
    rate_limiter_backend_registry.register(backend_1)
    rate_limiter_backend_registry.register(backend_2)
    assert rate_limiter_backend_registry.get() is backend_2

    await backend_1.__aexit__(None, None, None)
    assert rate_limiter_backend_registry.get() is backend_2


# --- Sync (SQLite) ---


async def test_sync_sqlite_backend_round_trip(tmp_path) -> None:  # noqa: ANN001
    """``async with`` registers on enter and unregisters on exit."""
    db_path = tmp_path / "lock.db"
    async with SQLiteSyncBackend(db_path):
        assert sync_backend_registry.is_loaded is True
    assert sync_backend_registry.is_loaded is False


# --- Sync (Redis) ---


async def test_sync_redis_backend_round_trip(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """``async with`` registers on enter and unregisters on exit."""
    async with RedisSyncBackend("redis://localhost"):
        assert sync_backend_registry.is_loaded is True
    assert sync_backend_registry.is_loaded is False


# --- Sync (PostgreSQL) ---


async def test_sync_postgres_backend_round_trip(
    mocker: MockerFixture,
) -> None:
    """``async with`` registers on enter and unregisters on exit."""
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


async def test_rate_limiter_redis_backend_round_trip(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """``async with`` registers on enter and unregisters on exit."""
    async with RedisRateLimiterBackend("redis://localhost"):
        assert rate_limiter_backend_registry.is_loaded is True
    assert rate_limiter_backend_registry.is_loaded is False


# --- Cache (Redis) ---


async def test_cache_redis_backend_round_trip(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """``async with`` registers on enter and unregisters on exit."""
    async with RedisCacheBackend("redis://localhost"):
        assert cache_backend_registry.is_loaded is True
    assert cache_backend_registry.is_loaded is False


# --- Cache (Memory) ---


async def test_cache_memory_backend_round_trip() -> None:
    """``async with`` registers on enter and unregisters on exit."""
    async with MemoryCacheBackend():
        assert cache_backend_registry.is_loaded is True
    assert cache_backend_registry.is_loaded is False


async def test_cache_memory_backend_does_not_evict_replacement() -> None:
    """Exiting an older cache backend leaves a newer one in place."""
    backend_1 = MemoryCacheBackend()
    backend_2 = MemoryCacheBackend()
    cache_backend_registry.register(backend_1)
    cache_backend_registry.register(backend_2)
    assert cache_backend_registry.get() is backend_2

    await backend_1.__aexit__(None, None, None)
    assert cache_backend_registry.get() is backend_2


# --- Sync (Kubernetes) ---


async def test_sync_kubernetes_backend_round_trip(
    mocker: MockerFixture,
) -> None:
    """``async with`` registers on enter and unregisters on exit."""
    mock_client = MagicMock()
    mock_client.close = AsyncMock()

    async def _empty_list(*_args: object, **_kwargs: object):  # noqa: ANN202
        return
        yield  # pragma: no cover

    mock_client.list = _empty_list
    mocker.patch(
        "grelmicro.sync.kubernetes.AsyncClient",
        return_value=mock_client,
    )

    async with KubernetesSyncBackend(namespace="default"):
        assert sync_backend_registry.is_loaded is True
    assert sync_backend_registry.is_loaded is False


# --- Identity check on unregister ---


def test_sync_registry_unregister_identity() -> None:
    """register(a); register(b); unregister(a) leaves b as current."""
    a = MemorySyncBackend()
    b = MemorySyncBackend()
    sync_backend_registry.register(a)
    sync_backend_registry.register(b)
    sync_backend_registry.unregister(a)
    assert sync_backend_registry.get() is b


def test_cache_registry_unregister_identity() -> None:
    """register(a); register(b); unregister(a) leaves b as current."""
    a = MemoryCacheBackend()
    b = MemoryCacheBackend()
    cache_backend_registry.register(a)
    cache_backend_registry.register(b)
    cache_backend_registry.unregister(a)
    assert cache_backend_registry.get() is b


def test_rate_limiter_registry_unregister_identity() -> None:
    """register(a); register(b); unregister(a) leaves b as current."""
    a = MemoryRateLimiterBackend()
    b = MemoryRateLimiterBackend()
    rate_limiter_backend_registry.register(a)
    rate_limiter_backend_registry.register(b)
    rate_limiter_backend_registry.unregister(a)
    assert rate_limiter_backend_registry.get() is b


def test_resilience_use_backend_registers() -> None:
    """`resilience.use_backend` installs the rate limiter backend."""
    rate_limiter_backend_registry.reset()
    backend = MemoryRateLimiterBackend()
    resilience_use_backend(backend)
    assert rate_limiter_backend_registry.get() is backend


def test_register_same_instance_is_noop() -> None:
    """Re-registering the same instance does not warn or replace."""
    backend = MemorySyncBackend()
    sync_backend_registry.register(backend)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        sync_backend_registry.register(backend)
    assert sync_backend_registry.get() is backend
