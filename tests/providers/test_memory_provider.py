"""Tests for the Memory Provider."""

import pytest

from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.providers.memory import MemoryProvider
from grelmicro.resilience import CircuitBreakerRegistry, RateLimiterRegistry
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter


def test_short_name() -> None:
    """The provider carries the `memory` short name."""
    assert MemoryProvider.short_name == "memory"


def test_repr() -> None:
    """`repr` shows the provider with no arguments."""
    assert repr(MemoryProvider()) == "MemoryProvider()"


def test_factories_return_adapters() -> None:
    """The provider builds an adapter for every supported component."""
    provider = MemoryProvider()
    assert isinstance(provider.lock(), MemoryLockAdapter)
    assert isinstance(provider.leaderelection(), MemoryLeaderElectionAdapter)
    assert isinstance(provider.schedule(), MemoryScheduleAdapter)
    assert isinstance(provider.cache(), MemoryCacheAdapter)
    assert isinstance(provider.ratelimiter(), MemoryRateLimiterAdapter)
    assert isinstance(provider.circuitbreaker(), MemoryCircuitBreakerAdapter)


def test_factories_cache_one_adapter_per_kind() -> None:
    """Each factory returns the same instance on repeated calls."""
    provider = MemoryProvider()
    assert provider.lock() is provider.lock()
    assert provider.leaderelection() is provider.leaderelection()
    assert provider.schedule() is provider.schedule()
    assert provider.cache() is provider.cache()
    assert provider.ratelimiter() is provider.ratelimiter()
    assert provider.circuitbreaker() is provider.circuitbreaker()


def test_unknown_kwarg_raises() -> None:
    """A stray kwarg is forwarded and errors on first creation."""
    provider = MemoryProvider()
    with pytest.raises(TypeError):
        provider.lock(bogus=1)


async def test_handles_share_lock_state() -> None:
    """Two handles from the same provider observe shared lock state."""
    provider = MemoryProvider()
    one = provider.lock()
    two = provider.lock()
    assert one is two
    async with one:
        assert await one.acquire(name="cart", token="w1", duration=10)
        assert await two.locked(name="cart") is True
        assert await two.owned(name="cart", token="w1") is True


async def test_check_returns_none() -> None:
    """`check` reports the in-process backend as ready."""
    assert await MemoryProvider().check() is None


async def test_context_manager_is_no_op() -> None:
    """The provider enters and exits without owning a resource."""
    provider = MemoryProvider()
    async with provider as opened:
        assert opened is provider
    # Adapters are still cached after exit: the components own their lifecycle.
    assert provider.lock() is provider.lock()


def test_coordination_resolves_backends_from_provider() -> None:
    """`Coordination(memory)` resolves lock, election, and schedule backends."""
    provider = MemoryProvider()
    coordination = Coordination(provider)
    assert isinstance(coordination.lock_backend, MemoryLockAdapter)
    assert isinstance(
        coordination.election_backend, MemoryLeaderElectionAdapter
    )
    assert isinstance(coordination.schedule_backend, MemoryScheduleAdapter)


def test_cache_resolves_backend_from_provider() -> None:
    """`Cache(memory)` resolves a cache backend from the provider."""
    provider = MemoryProvider()
    cache = Cache(provider)
    assert isinstance(cache.backend, MemoryCacheAdapter)


def test_ratelimiter_registry_resolves_backend_from_provider() -> None:
    """`RateLimiterRegistry(memory)` resolves a rate limiter backend."""
    provider = MemoryProvider()
    registry = RateLimiterRegistry(provider)
    assert isinstance(registry.backend, MemoryRateLimiterAdapter)


def test_circuitbreaker_registry_resolves_backend_from_provider() -> None:
    """`CircuitBreakerRegistry(memory)` resolves a circuit breaker backend."""
    provider = MemoryProvider()
    registry = CircuitBreakerRegistry(provider)
    assert isinstance(registry.backend, MemoryCircuitBreakerAdapter)
