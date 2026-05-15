"""Backend lifecycle: standalone async-with round-trips for first-party adapters."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience.redis import RedisRateLimiterAdapter
from grelmicro.sync.kubernetes import KubernetesSyncAdapter
from grelmicro.sync.memory import MemorySyncAdapter
from grelmicro.sync.postgres import PostgresSyncAdapter
from grelmicro.sync.redis import RedisSyncAdapter
from grelmicro.sync.sqlite import SQLiteSyncAdapter


@pytest.fixture
def mock_redis(mocker: MockerFixture) -> MagicMock:
    """Mock the Redis client built by `RedisProvider`."""
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    mock_client.register_script = MagicMock(return_value=AsyncMock())
    mocker.patch(
        "grelmicro.providers.redis.Redis.from_url",
        return_value=mock_client,
    )
    return mock_client


async def test_sync_memory_async_with() -> None:
    """The memory sync adapter opens and closes cleanly."""
    async with MemorySyncAdapter():
        pass


async def test_cache_memory_async_with() -> None:
    """The memory cache adapter opens and closes cleanly."""
    async with MemoryCacheAdapter():
        pass


async def test_sync_redis_async_with(mock_redis: MagicMock) -> None:  # noqa: ARG001
    """The Redis sync adapter opens and closes cleanly."""
    async with RedisSyncAdapter(provider=RedisProvider("redis://localhost")):
        pass


async def test_sync_postgres_async_with(
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Postgres sync adapter opens and closes cleanly."""
    monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/db")
    mock_pool = MagicMock()
    mock_pool.execute = AsyncMock()
    mock_pool.close = AsyncMock()
    mocker.patch(
        "grelmicro.providers.postgres.create_pool",
        AsyncMock(return_value=mock_pool),
    )
    async with PostgresSyncAdapter():
        pass


async def test_sync_sqlite_async_with(tmp_path) -> None:  # noqa: ANN001
    """The SQLite sync adapter opens and closes cleanly."""
    async with SQLiteSyncAdapter(tmp_path / "lock.db"):
        pass


async def test_sync_kubernetes_async_with(mocker: MockerFixture) -> None:
    """The Kubernetes sync adapter opens and closes cleanly."""
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
    async with KubernetesSyncAdapter(namespace="default"):
        pass


async def test_cache_redis_async_with(mock_redis: MagicMock) -> None:  # noqa: ARG001
    """The Redis cache adapter opens and closes cleanly."""
    async with RedisCacheAdapter(provider=RedisProvider("redis://localhost")):
        pass


async def test_rate_limiter_redis_async_with(mock_redis: MagicMock) -> None:  # noqa: ARG001
    """The Redis rate limiter adapter opens and closes cleanly."""
    async with RedisRateLimiterAdapter(
        provider=RedisProvider("redis://localhost")
    ):
        pass
