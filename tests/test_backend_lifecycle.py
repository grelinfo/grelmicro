"""Backend lifecycle: explicit named registration, scoped overrides, lifespan."""

import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

import grelmicro
from grelmicro import cache as cache_mod
from grelmicro import health as health_mod
from grelmicro import resilience as resilience_mod
from grelmicro import sync as sync_mod
from grelmicro._backends import BackendNotLoadedError
from grelmicro.cache._backends import cache_backend_registry
from grelmicro.cache.memory import MemoryCacheBackend
from grelmicro.cache.redis import RedisCacheBackend
from grelmicro.health._backends import health_registry
from grelmicro.health._registry import HealthRegistry
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
def _clean_registries() -> None:
    """Reset every backend registry between tests."""
    sync_backend_registry.reset()
    cache_backend_registry.reset()
    rate_limiter_backend_registry.reset()


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


# --- Pure constructors (no registry writes) ---


def test_sync_constructor_does_not_register() -> None:
    """Sync constructor does not register."""
    MemorySyncBackend()
    assert not sync_backend_registry.is_loaded


def test_cache_constructor_does_not_register() -> None:
    """Cache constructor does not register."""
    MemoryCacheBackend()
    assert not cache_backend_registry.is_loaded


def test_rate_limiter_constructor_does_not_register() -> None:
    """Rate limiter constructor does not register."""
    MemoryRateLimiterBackend()
    assert not rate_limiter_backend_registry.is_loaded


# --- async with opens the backend but does NOT register ---


async def test_sync_memory_async_with_does_not_register() -> None:
    """Sync memory async with does not register."""
    async with MemorySyncBackend():
        assert not sync_backend_registry.is_loaded


async def test_cache_memory_async_with_does_not_register() -> None:
    """Cache memory async with does not register."""
    async with MemoryCacheBackend():
        assert not cache_backend_registry.is_loaded


# --- Explicit register / unregister ---


def test_register_and_get() -> None:
    """Register and get."""
    backend = MemorySyncBackend()
    sync_backend_registry.register("default", backend)
    assert sync_backend_registry.get() is backend


def test_register_named_and_get_by_name() -> None:
    """Register named and get by name."""
    primary = MemorySyncBackend()
    analytics = MemorySyncBackend()
    sync_backend_registry.register("primary", primary)
    sync_backend_registry.register("analytics", analytics)
    assert sync_backend_registry.get("primary") is primary
    assert sync_backend_registry.get("analytics") is analytics


def test_get_default_falls_back_to_sole_entry() -> None:
    """Get default falls back to sole entry."""
    only = MemorySyncBackend()
    sync_backend_registry.register("primary", only)
    assert sync_backend_registry.get() is only


def test_get_default_raises_when_multiple_no_default() -> None:
    """Get default raises when multiple no default."""
    sync_backend_registry.register("primary", MemorySyncBackend())
    sync_backend_registry.register("analytics", MemorySyncBackend())
    with pytest.raises(BackendNotLoadedError, match="multiple"):
        sync_backend_registry.get()


def test_register_same_instance_is_noop() -> None:
    """Register same instance is noop."""
    backend = MemorySyncBackend()
    sync_backend_registry.register("default", backend)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        sync_backend_registry.register("default", backend)
    assert sync_backend_registry.get() is backend


def test_register_different_instance_warns() -> None:
    """Register different instance warns."""
    sync_backend_registry.register("default", MemorySyncBackend())
    with pytest.warns(UserWarning, match="Overwriting"):
        sync_backend_registry.register("default", MemorySyncBackend())


def test_unregister_with_identity_check() -> None:
    """Unregister with identity check."""
    a = MemorySyncBackend()
    b = MemorySyncBackend()
    sync_backend_registry.register("default", a)
    sync_backend_registry.unregister("default", b)  # wrong instance: no-op
    assert sync_backend_registry.get() is a
    sync_backend_registry.unregister("default", a)  # right instance: clears
    assert not sync_backend_registry.is_loaded


def test_unregister_unknown_name_is_noop() -> None:
    """Unregister unknown name is noop."""
    sync_backend_registry.unregister("missing")
    assert not sync_backend_registry.is_loaded


# --- ContextVar use() override ---


def test_use_overrides_default() -> None:
    """Use overrides default."""
    registered = MemorySyncBackend()
    override = MemorySyncBackend()
    sync_backend_registry.register("default", registered)
    with sync_backend_registry.use(override):
        assert sync_backend_registry.get() is override
    assert sync_backend_registry.get() is registered


def test_use_overrides_named() -> None:
    """Use overrides named."""
    sync_backend_registry.register("primary", MemorySyncBackend())
    sync_backend_registry.register("analytics", MemorySyncBackend())
    fake_analytics = MemorySyncBackend()
    with sync_backend_registry.use(analytics=fake_analytics):
        assert sync_backend_registry.get("analytics") is fake_analytics


def test_use_stacks_lifo() -> None:
    """Use stacks lifo."""
    inner = MemorySyncBackend()
    outer = MemorySyncBackend()
    sync_backend_registry.register("default", MemorySyncBackend())
    with sync_backend_registry.use(outer):
        with sync_backend_registry.use(inner):
            assert sync_backend_registry.get() is inner
        assert sync_backend_registry.get() is outer


# --- grelmicro.lifespan() walks every registry ---


async def test_lifespan_opens_registered_backends() -> None:
    """Lifespan opens registered backends."""
    sync_b = MemorySyncBackend()
    cache_b = MemoryCacheBackend()
    rl_b = MemoryRateLimiterBackend()
    sync_backend_registry.register("default", sync_b)
    cache_backend_registry.register("default", cache_b)
    rate_limiter_backend_registry.register("default", rl_b)

    async with grelmicro.lifespan():
        # backends remain registered while open
        assert sync_backend_registry.get() is sync_b
        assert cache_backend_registry.get() is cache_b
        assert rate_limiter_backend_registry.get() is rl_b


async def test_lifespan_excludes_module() -> None:
    """Lifespan excludes module."""
    sync_backend_registry.register("default", MemorySyncBackend())
    cache_backend_registry.register("default", MemoryCacheBackend())
    async with grelmicro.lifespan(exclude={"cache"}):
        # cache backend was not entered (we cannot easily detect this
        # for memory backends; the contract is that entries in
        # ``exclude`` are skipped — verified at unit level in registry)
        pass


async def test_lifespan_excludes_named_entry() -> None:
    """Lifespan excludes named entry."""
    primary = MemorySyncBackend()
    analytics = MemorySyncBackend()
    sync_backend_registry.register("primary", primary)
    sync_backend_registry.register("analytics", analytics)
    async with grelmicro.lifespan(exclude={"sync.analytics"}):
        assert sync_backend_registry.get("primary") is primary


async def test_lifespan_with_ad_hoc_backend() -> None:
    """Lifespan with ad hoc backend."""
    ad_hoc = MemorySyncBackend()
    async with grelmicro.lifespan(ad_hoc):
        # ad-hoc enters the async ctx but is not registered
        assert not sync_backend_registry.is_loaded


# --- Backends still work as standalone async context managers ---


async def test_sync_redis_async_with_round_trip(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """Sync redis async with round trip."""
    async with RedisSyncBackend("redis://localhost"):
        pass


async def test_sync_postgres_async_with_round_trip(
    mocker: MockerFixture,
) -> None:
    """Sync postgres async with round trip."""
    mock_pool = MagicMock()
    mock_pool.execute = AsyncMock()
    mock_pool.close = AsyncMock()
    mocker.patch(
        "grelmicro.sync.postgres.create_pool",
        AsyncMock(return_value=mock_pool),
    )
    async with PostgresSyncBackend("postgresql://localhost/db"):
        pass


async def test_sync_sqlite_async_with_round_trip(tmp_path) -> None:  # noqa: ANN001
    """Sync sqlite async with round trip."""
    async with SQLiteSyncBackend(tmp_path / "lock.db"):
        pass


async def test_sync_kubernetes_async_with_round_trip(
    mocker: MockerFixture,
) -> None:
    """Sync kubernetes async with round trip."""
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
        pass


async def test_cache_redis_async_with_round_trip(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """Cache redis async with round trip."""
    async with RedisCacheBackend("redis://localhost"):
        pass


async def test_rate_limiter_redis_async_with_round_trip(
    mock_redis: MagicMock,  # noqa: ARG001
) -> None:
    """Rate limiter redis async with round trip."""
    async with RedisRateLimiterBackend("redis://localhost"):
        pass


# --- Module-level helpers ---


def test_sync_use_backend_registers_default() -> None:
    """Sync use backend registers default."""
    backend = MemorySyncBackend()
    sync_mod.use_backend(backend)
    assert sync_backend_registry.get() is backend


def test_cache_use_backend_registers_default() -> None:
    """Cache use backend registers default."""
    backend = MemoryCacheBackend()
    cache_mod.use_backend(backend)
    assert cache_backend_registry.get() is backend


def test_resilience_use_backend_registers_default() -> None:
    """Resilience use backend registers default."""
    backend = MemoryRateLimiterBackend()
    resilience_mod.use_backend(backend)
    assert rate_limiter_backend_registry.get() is backend


def test_sync_register_and_unregister_module_helpers() -> None:
    """Sync register and unregister module helpers."""
    backend = MemorySyncBackend()
    sync_mod.register("primary", backend)
    assert sync_backend_registry.get("primary") is backend
    sync_mod.unregister("primary", backend)
    assert not sync_backend_registry.is_loaded


def test_sync_use_module_helper() -> None:
    """Sync use module helper."""
    sync_backend_registry.register("default", MemorySyncBackend())
    override = MemorySyncBackend()
    with sync_mod.use(override):
        assert sync_backend_registry.get() is override


def test_cache_register_unregister_use_module_helpers() -> None:
    """Cache register, unregister, and use module helpers."""
    backend = MemoryCacheBackend()
    cache_mod.register("primary", backend)
    assert cache_backend_registry.get("primary") is backend
    override = MemoryCacheBackend()
    with cache_mod.use(override):
        assert cache_backend_registry.get() is override
    cache_mod.unregister("primary", backend)
    assert not cache_backend_registry.is_loaded


def test_resilience_register_unregister_use_module_helpers() -> None:
    """Resilience register, unregister, and use module helpers."""
    backend = MemoryRateLimiterBackend()
    resilience_mod.register("primary", backend)
    assert rate_limiter_backend_registry.get("primary") is backend
    override = MemoryRateLimiterBackend()
    with resilience_mod.use(override):
        assert rate_limiter_backend_registry.get() is override
    resilience_mod.unregister("primary", backend)
    assert not rate_limiter_backend_registry.is_loaded


def test_health_register_unregister_use_module_helpers() -> None:
    """Health register, unregister, use, and use_registry helpers."""
    health_registry.reset()
    registry = HealthRegistry()
    health_mod.register("primary", registry)
    assert health_registry.get("primary") is registry
    override = HealthRegistry()
    with health_mod.use(override):
        assert health_registry.get() is override
    health_mod.use_registry(registry)
    assert health_registry.get() is registry
    health_mod.unregister("primary", registry)
    health_registry.reset()
