"""DeprecationWarning coverage for legacy module-level helpers (issue #206)."""

import pytest

import grelmicro
from grelmicro import cache as cache_mod
from grelmicro import health as health_mod
from grelmicro import resilience as resilience_mod
from grelmicro import sync as sync_mod
from grelmicro.cache._backends import cache_backend_registry
from grelmicro.cache.memory import MemoryCacheBackend
from grelmicro.health._backends import health_registry
from grelmicro.health._registry import HealthRegistry
from grelmicro.resilience._backends import (
    circuit_breaker_backend_registry,
    rate_limiter_backend_registry,
)
from grelmicro.resilience.memory import (
    MemoryCircuitBreakerBackend,
    MemoryRateLimiterBackend,
)
from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync.memory import MemorySyncBackend


@pytest.fixture(autouse=True)
def _clean_registries() -> None:
    """Reset every backend registry between tests."""
    sync_backend_registry.reset()
    cache_backend_registry.reset()
    rate_limiter_backend_registry.reset()
    circuit_breaker_backend_registry.reset()
    health_registry.reset()


def test_sync_use_backend_warns() -> None:
    """`grelmicro.sync.use_backend` warns and points at `Grelmicro`."""
    with pytest.warns(DeprecationWarning, match="grelmicro.sync.use_backend"):
        sync_mod.use_backend(MemorySyncBackend())


def test_sync_register_warns() -> None:
    """`grelmicro.sync.register` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.sync.register"):
        sync_mod.register(MemorySyncBackend(), "primary")


def test_sync_unregister_warns() -> None:
    """`grelmicro.sync.unregister` warns."""
    sync_backend_registry.register(MemorySyncBackend(), "primary")
    with pytest.warns(DeprecationWarning, match="grelmicro.sync.unregister"):
        sync_mod.unregister("primary")


def test_sync_use_warns() -> None:
    """`grelmicro.sync.use` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.sync.use"):
        cm = sync_mod.use(MemorySyncBackend())
    with cm:
        pass


def test_cache_use_backend_warns() -> None:
    """`grelmicro.cache.use_backend` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.cache.use_backend"):
        cache_mod.use_backend(MemoryCacheBackend())


def test_cache_register_warns() -> None:
    """`grelmicro.cache.register` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.cache.register"):
        cache_mod.register(MemoryCacheBackend(), "primary")


def test_cache_unregister_warns() -> None:
    """`grelmicro.cache.unregister` warns."""
    cache_backend_registry.register(MemoryCacheBackend(), "primary")
    with pytest.warns(DeprecationWarning, match="grelmicro.cache.unregister"):
        cache_mod.unregister("primary")


def test_cache_use_warns() -> None:
    """`grelmicro.cache.use` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.cache.use"):
        cm = cache_mod.use(MemoryCacheBackend())
    with cm:
        pass


def test_health_use_registry_warns() -> None:
    """`grelmicro.health.use_registry` warns."""
    with pytest.warns(
        DeprecationWarning, match="grelmicro.health.use_registry"
    ):
        health_mod.use_registry(HealthRegistry())


def test_health_register_warns() -> None:
    """`grelmicro.health.register` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.health.register"):
        health_mod.register(HealthRegistry(), "primary")


def test_health_unregister_warns() -> None:
    """`grelmicro.health.unregister` warns."""
    health_registry.register(HealthRegistry(), "primary")
    with pytest.warns(DeprecationWarning, match="grelmicro.health.unregister"):
        health_mod.unregister("primary")


def test_health_use_warns() -> None:
    """`grelmicro.health.use` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.health.use"):
        cm = health_mod.use(HealthRegistry())
    with cm:
        pass


def test_resilience_use_backend_warns() -> None:
    """`grelmicro.resilience.use_backend` warns."""
    with pytest.warns(
        DeprecationWarning, match="grelmicro.resilience.use_backend"
    ):
        resilience_mod.use_backend(MemoryRateLimiterBackend())


def test_resilience_register_warns() -> None:
    """`grelmicro.resilience.register` warns."""
    with pytest.warns(
        DeprecationWarning, match="grelmicro.resilience.register"
    ):
        resilience_mod.register(MemoryRateLimiterBackend(), "primary")


def test_resilience_unregister_warns() -> None:
    """`grelmicro.resilience.unregister` warns."""
    rate_limiter_backend_registry.register(
        MemoryRateLimiterBackend(), "primary"
    )
    with pytest.warns(
        DeprecationWarning, match="grelmicro.resilience.unregister"
    ):
        resilience_mod.unregister("primary")


def test_resilience_use_warns() -> None:
    """`grelmicro.resilience.use` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.resilience.use"):
        cm = resilience_mod.use(MemoryRateLimiterBackend())
    with cm:
        pass


def test_resilience_register_circuit_breaker_warns() -> None:
    """`grelmicro.resilience.register_circuit_breaker` warns."""
    with pytest.warns(
        DeprecationWarning,
        match="grelmicro.resilience.register_circuit_breaker",
    ):
        resilience_mod.register_circuit_breaker(
            MemoryCircuitBreakerBackend(), "primary"
        )


def test_resilience_use_circuit_breaker_backend_warns() -> None:
    """`grelmicro.resilience.use_circuit_breaker_backend` warns."""
    with pytest.warns(
        DeprecationWarning,
        match="grelmicro.resilience.use_circuit_breaker_backend",
    ):
        resilience_mod.use_circuit_breaker_backend(
            MemoryCircuitBreakerBackend()
        )


def test_resilience_unregister_circuit_breaker_warns() -> None:
    """`grelmicro.resilience.unregister_circuit_breaker` warns."""
    circuit_breaker_backend_registry.register(
        MemoryCircuitBreakerBackend(), "primary"
    )
    with pytest.warns(
        DeprecationWarning,
        match="grelmicro.resilience.unregister_circuit_breaker",
    ):
        resilience_mod.unregister_circuit_breaker("primary")


async def test_lifespan_warns() -> None:
    """The free `grelmicro.lifespan()` warns."""
    with pytest.warns(DeprecationWarning, match="grelmicro.lifespan"):
        async with grelmicro.lifespan():
            pass
