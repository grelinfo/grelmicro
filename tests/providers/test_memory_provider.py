"""Tests for the Memory Provider."""

from datetime import timedelta

import pytest

from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.providers.memory import MemoryProvider
from grelmicro.resilience import CircuitBreakerComponent, RateLimiterComponent
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
    assert isinstance(provider.lock_backend(), MemoryLockAdapter)
    assert isinstance(
        provider.leaderelection_backend(), MemoryLeaderElectionAdapter
    )
    assert isinstance(provider.schedule_backend(), MemoryScheduleAdapter)
    assert isinstance(provider.cache_backend(), MemoryCacheAdapter)
    assert isinstance(provider.ratelimiter_backend(), MemoryRateLimiterAdapter)
    assert isinstance(
        provider.circuitbreaker_backend(), MemoryCircuitBreakerAdapter
    )
    assert isinstance(provider.outbox_backend(), MemoryOutboxAdapter)


def test_factories_cache_one_adapter_per_kind() -> None:
    """Each factory returns the same instance on repeated calls."""
    provider = MemoryProvider()
    assert provider.lock_backend() is provider.lock_backend()
    assert (
        provider.leaderelection_backend() is provider.leaderelection_backend()
    )
    assert provider.schedule_backend() is provider.schedule_backend()
    assert provider.cache_backend() is provider.cache_backend()
    assert provider.ratelimiter_backend() is provider.ratelimiter_backend()
    assert (
        provider.circuitbreaker_backend() is provider.circuitbreaker_backend()
    )
    assert provider.outbox_backend() is provider.outbox_backend()


def test_outbox_ignores_the_settings_of_a_sql_store() -> None:
    """`table`, `auto_migrate` and `notify` describe a database, not a dict.

    `Outbox` passes them to whichever provider it holds, so the memory
    provider has to take them and carry on.
    """
    provider = MemoryProvider()

    adapter = provider.outbox_backend(
        table="outbox", auto_migrate=True, notify=True
    )

    assert isinstance(adapter, MemoryOutboxAdapter)


def test_outbox_rejects_a_keyword_it_does_not_know() -> None:
    """A stray keyword raises here as it does on every other factory."""
    provider = MemoryProvider()

    with pytest.raises(TypeError, match="bogus"):
        provider.outbox_backend(bogus="outbox")


def test_unknown_kwarg_raises() -> None:
    """A stray kwarg is forwarded and errors on first creation."""
    provider = MemoryProvider()
    with pytest.raises(TypeError):
        provider.lock_backend(bogus=1)


async def test_handles_share_lock_state() -> None:
    """Two handles from the same provider observe shared lock state."""
    provider = MemoryProvider()
    one = provider.lock_backend()
    two = provider.lock_backend()
    assert one is two
    async with one:
        assert await one.acquire(
            name="cart", token="w1", duration=timedelta(seconds=10)
        )
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
    assert provider.lock_backend() is provider.lock_backend()


def test_coordination_resolves_backends_from_provider() -> None:
    """`Coordination(memory)` resolves lock, election, and schedule backends."""
    provider = MemoryProvider()
    coordination = Coordination(provider)
    assert isinstance(coordination.lock_backend, MemoryLockAdapter)
    assert isinstance(
        coordination.leaderelection_backend, MemoryLeaderElectionAdapter
    )
    assert isinstance(coordination.schedule_backend, MemoryScheduleAdapter)


def test_cache_resolves_backend_from_provider() -> None:
    """`Cache(memory)` resolves a cache backend from the provider."""
    provider = MemoryProvider()
    cache = Cache(provider)
    assert isinstance(cache.backend, MemoryCacheAdapter)


def test_ratelimiter_component_resolves_backend_from_provider() -> None:
    """`RateLimiterComponent(memory)` resolves a rate limiter backend."""
    provider = MemoryProvider()
    component = RateLimiterComponent(provider)
    assert isinstance(component.backend, MemoryRateLimiterAdapter)


def test_circuitbreaker_component_resolves_backend_from_provider() -> None:
    """`CircuitBreakerComponent(memory)` resolves a circuit breaker backend."""
    provider = MemoryProvider()
    component = CircuitBreakerComponent(provider)
    assert isinstance(component.backend, MemoryCircuitBreakerAdapter)
