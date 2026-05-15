"""Backend lifecycle: standalone async-with round-trips and the remaining internal registries."""

import warnings
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from grelmicro._backends import (
    BackendAlreadyRegisteredError,
    BackendNotLoadedError,
)
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.health._backends import health_checks
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience._backends import (
    circuit_breaker_backend_registry,
    rate_limiter_backend_registry,
)
from grelmicro.resilience.memory import (
    MemoryRateLimiterAdapter,
)
from grelmicro.resilience.redis import RedisRateLimiterAdapter
from grelmicro.sync.kubernetes import KubernetesSyncAdapter
from grelmicro.sync.memory import MemorySyncAdapter
from grelmicro.sync.postgres import PostgresSyncAdapter
from grelmicro.sync.redis import RedisSyncAdapter
from grelmicro.sync.sqlite import SQLiteSyncAdapter


@pytest.fixture(autouse=True)
def _clean_registries() -> None:
    """Reset the remaining internal registries between tests."""
    rate_limiter_backend_registry.reset()
    circuit_breaker_backend_registry.reset()
    health_checks.reset()


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


# --- Standalone async-with round-trips for first-party adapters ---


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


# --- Remaining internal BackendRegistry semantics ---
# These cover `rate_limiter_backend_registry`, `circuit_breaker_backend_registry`,
# and `health_checks`. The sync and cache registries have been removed: those
# kinds resolve through the `Grelmicro` app (see `tests/test_grelmicro_app.py`).


def test_register_and_get() -> None:
    """Register and get."""
    backend = MemoryRateLimiterAdapter()
    rate_limiter_backend_registry.register(backend, "default")
    assert rate_limiter_backend_registry.get() is backend


def test_register_named_and_get_by_name() -> None:
    """Register named and get by name."""
    primary = MemoryRateLimiterAdapter()
    analytics = MemoryRateLimiterAdapter()
    rate_limiter_backend_registry.register(primary, "primary")
    rate_limiter_backend_registry.register(analytics, "analytics")
    assert rate_limiter_backend_registry.get("primary") is primary
    assert rate_limiter_backend_registry.get("analytics") is analytics


def test_get_default_falls_back_to_sole_entry() -> None:
    """Get default falls back to sole entry."""
    only = MemoryRateLimiterAdapter()
    rate_limiter_backend_registry.register(only, "primary")
    assert rate_limiter_backend_registry.get() is only


def test_get_default_raises_when_multiple_no_default() -> None:
    """Get default raises when multiple no default."""
    rate_limiter_backend_registry.register(
        MemoryRateLimiterAdapter(), "primary"
    )
    rate_limiter_backend_registry.register(
        MemoryRateLimiterAdapter(), "analytics"
    )
    with pytest.raises(BackendNotLoadedError, match="multiple"):
        rate_limiter_backend_registry.get()


def test_register_same_instance_is_noop() -> None:
    """Register same instance is noop."""
    backend = MemoryRateLimiterAdapter()
    rate_limiter_backend_registry.register(backend, "default")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        rate_limiter_backend_registry.register(backend, "default")
    assert rate_limiter_backend_registry.get() is backend


def test_register_different_instance_raises() -> None:
    """Register different instance raises BackendAlreadyRegisteredError."""
    rate_limiter_backend_registry.register(
        MemoryRateLimiterAdapter(), "default"
    )
    with pytest.raises(BackendAlreadyRegisteredError):
        rate_limiter_backend_registry.register(
            MemoryRateLimiterAdapter(), "default"
        )


def test_unregister_with_identity_check() -> None:
    """Unregister with identity check."""
    a = MemoryRateLimiterAdapter()
    b = MemoryRateLimiterAdapter()
    rate_limiter_backend_registry.register(a, "default")
    rate_limiter_backend_registry.unregister("default", b)  # wrong: no-op
    assert rate_limiter_backend_registry.get() is a
    rate_limiter_backend_registry.unregister("default", a)  # right: clears
    assert not rate_limiter_backend_registry.is_loaded


def test_unregister_unknown_name_is_noop() -> None:
    """Unregister unknown name is noop."""
    rate_limiter_backend_registry.unregister("missing")
    assert not rate_limiter_backend_registry.is_loaded


def test_get_unknown_name_raises() -> None:
    """`get('missing')` raises `BackendNotLoadedError` when nothing matches."""
    rate_limiter_backend_registry.register(
        MemoryRateLimiterAdapter(), "primary"
    )
    with pytest.raises(BackendNotLoadedError, match="missing"):
        rate_limiter_backend_registry.get("missing")


def test_use_overrides_default() -> None:
    """`registry.use(b)` swaps the default slot inside the block."""
    registered = MemoryRateLimiterAdapter()
    override = MemoryRateLimiterAdapter()
    rate_limiter_backend_registry.register(registered, "default")
    with rate_limiter_backend_registry.use(override):
        assert rate_limiter_backend_registry.get() is override
    assert rate_limiter_backend_registry.get() is registered


def test_use_overrides_named() -> None:
    """`registry.use(name=b)` swaps a named slot."""
    rate_limiter_backend_registry.register(
        MemoryRateLimiterAdapter(), "primary"
    )
    rate_limiter_backend_registry.register(
        MemoryRateLimiterAdapter(), "analytics"
    )
    fake_analytics = MemoryRateLimiterAdapter()
    with rate_limiter_backend_registry.use(analytics=fake_analytics):
        assert rate_limiter_backend_registry.get("analytics") is fake_analytics


def test_use_stacks_lifo() -> None:
    """Nested `use(...)` blocks stack LIFO."""
    inner = MemoryRateLimiterAdapter()
    outer = MemoryRateLimiterAdapter()
    rate_limiter_backend_registry.register(
        MemoryRateLimiterAdapter(), "default"
    )
    with rate_limiter_backend_registry.use(outer):
        with rate_limiter_backend_registry.use(inner):
            assert rate_limiter_backend_registry.get() is inner
        assert rate_limiter_backend_registry.get() is outer


class _CountingRateLimiterAdapter(MemoryRateLimiterAdapter):
    """`MemoryRateLimiterAdapter` that tracks `__aenter__` calls."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        await super().__aenter__()
        return self
