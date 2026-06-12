"""Tests for the forgiving `uses=[...]` / `use(...)` coercion rules.

Three shorthands are exercised here: a bare Component class, a bare Adapter
(instance or class), and a bare Provider that auto-registers the components it
serves. The explicit forms stay legal and always win over the implicit ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Self

import pytest

if TYPE_CHECKING:
    from types import TracebackType

from grelmicro import AmbiguousProviderError, Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import (
    MemoryLeaderElectionBackend,
    MemoryLockAdapter,
)
from grelmicro.providers import Provider
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.resilience._components import CircuitBreakers, RateLimiters
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter


class _MemoryProvider(Provider):
    """A Provider serving every kind from in-memory adapters.

    Each factory hands back a fresh memory adapter, so a bare
    `Grelmicro(uses=[_MemoryProvider()])` should auto-register one default
    Component per kind: coordination, cache, ratelimiter, circuitbreaker.
    """

    short_name: ClassVar[str] = "memory"

    def lock(self, **kwargs: object) -> MemoryLockAdapter:  # noqa: ARG002
        return MemoryLockAdapter()

    def leaderelection(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> MemoryLeaderElectionBackend:
        return MemoryLeaderElectionBackend()

    def cache(self, **kwargs: object) -> MemoryCacheAdapter:  # noqa: ARG002
        return MemoryCacheAdapter()

    def ratelimiter(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> MemoryRateLimiterAdapter:
        return MemoryRateLimiterAdapter()

    def circuitbreaker(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> MemoryCircuitBreakerAdapter:
        return MemoryCircuitBreakerAdapter()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _CacheOnlyProvider(Provider):
    """A Provider serving only the cache kind."""

    short_name: ClassVar[str] = "cacheonly"

    def cache(self, **kwargs: object) -> MemoryCacheAdapter:  # noqa: ARG002
        return MemoryCacheAdapter()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _RateLimiterOnlyProvider(Provider):
    """A Provider serving only the rate limiter kind, skipping cache."""

    short_name: ClassVar[str] = "ratelimiteronly"

    def ratelimiter(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> MemoryRateLimiterAdapter:
        return MemoryRateLimiterAdapter()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


# --- Bare Component class ---


def test_bare_component_class_constructs_with_defaults() -> None:
    """A Component class with all-defaulting args is instantiated for you."""

    class _DefaultComponent:
        kind: ClassVar[str] = "rec"

        def __init__(self, *, name: str = "default") -> None:
            self.name = name

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    micro = Grelmicro(uses=[_DefaultComponent])

    assert micro.get("rec").name == "default"


# --- Bare Adapter instance ---


def test_bare_adapter_instance_wraps_in_component() -> None:
    """A bare adapter instance is wrapped in its matching Component."""
    micro = Grelmicro(uses=[MemoryLockAdapter()])

    coordination = micro.get("coordination")
    assert isinstance(coordination, Coordination)
    assert coordination.lock_backend is not None


# --- Bare Adapter class ---


def test_bare_adapter_class_constructs_then_wraps() -> None:
    """A bare adapter class is instantiated and wrapped in its Component."""
    micro = Grelmicro(uses=[MemoryCircuitBreakerAdapter])

    breakers = micro.get("circuitbreaker")
    assert isinstance(breakers, CircuitBreakers)


def test_bare_adapter_class_matches_explicit_form() -> None:
    """`uses=[MemoryCacheAdapter]` matches `uses=[Cache(MemoryCacheAdapter())]`."""
    bare = Grelmicro(uses=[MemoryCacheAdapter])
    explicit = Grelmicro(uses=[Cache(MemoryCacheAdapter())])

    assert isinstance(bare.get("cache"), Cache)
    assert isinstance(explicit.get("cache"), Cache)


# --- Provider auto-registration ---


def test_provider_registers_one_component_per_served_kind() -> None:
    """A lone Provider auto-registers a default Component per kind it serves."""
    micro = Grelmicro(uses=[_MemoryProvider()])

    assert isinstance(micro.get("coordination"), Coordination)
    assert isinstance(micro.get("cache"), Cache)
    assert isinstance(micro.get("ratelimiter"), RateLimiters)
    assert isinstance(micro.get("circuitbreaker"), CircuitBreakers)


def test_provider_skips_kinds_it_does_not_serve() -> None:
    """A Provider serving only cache registers only the cache Component."""
    micro = Grelmicro(uses=[_CacheOnlyProvider()])

    assert isinstance(micro.get("cache"), Cache)
    kinds = {component.kind for component in micro.components}
    assert kinds == {"cache"}


def test_provider_skips_cache_when_only_ratelimiter_served() -> None:
    """A Provider serving only the rate limiter registers no cache Component."""
    micro = Grelmicro(uses=[_RateLimiterOnlyProvider()])

    assert isinstance(micro.get("ratelimiter"), RateLimiters)
    kinds = {component.kind for component in micro.components}
    assert kinds == {"ratelimiter"}


def test_valkey_provider_auto_registers_like_redis() -> None:
    """A bare `ValkeyProvider` auto-registers every kind it inherits from Redis."""
    micro = Grelmicro(uses=[ValkeyProvider("redis://localhost:6379/0")])

    assert isinstance(micro.get("coordination"), Coordination)
    assert isinstance(micro.get("cache"), Cache)
    assert isinstance(micro.get("ratelimiter"), RateLimiters)
    assert isinstance(micro.get("circuitbreaker"), CircuitBreakers)


async def test_provider_auto_registered_components_lifecycle() -> None:
    """The Provider is lifecycled once and its auto-registered Components open."""
    provider = _MemoryProvider()
    micro = Grelmicro(uses=[provider])

    async with micro:
        assert micro.get("cache") is not None


# --- Explicit wins ---


def test_explicit_component_disables_provider_auto_registration() -> None:
    """Any explicit Component turns provider auto-registration off entirely."""
    provider = _MemoryProvider()
    micro = Grelmicro(uses=[provider, Cache(MemoryCacheAdapter())])

    assert isinstance(micro.get("cache"), Cache)
    kinds = {component.kind for component in micro.components}
    assert kinds == {"cache"}


async def test_explicit_provider_listed_with_component_lifecycled_once() -> (
    None
):
    """A Provider listed beside an explicit Component is lifecycle-only."""
    provider = _MemoryProvider()
    micro = Grelmicro(uses=[provider, Coordination(lock=MemoryLockAdapter())])

    async with micro:
        pass

    kinds = {component.kind for component in micro.components}
    assert kinds == {"coordination"}


# --- Two-provider ambiguity ---


def test_two_providers_no_components_raises() -> None:
    """Two bare Providers with no Components make the default ambiguous."""
    with pytest.raises(AmbiguousProviderError, match="multiple providers"):
        Grelmicro(uses=[_MemoryProvider(), _CacheOnlyProvider()])


def test_two_providers_with_explicit_components_do_not_raise() -> None:
    """Explicit Components disable auto-registration, so two providers are fine."""
    micro = Grelmicro(
        uses=[
            _MemoryProvider(),
            _CacheOnlyProvider(),
            Cache(MemoryCacheAdapter()),
        ]
    )

    assert isinstance(micro.get("cache"), Cache)


# --- micro.use parity ---


def test_use_bare_adapter_class_parity() -> None:
    """`micro.use(MemoryCacheAdapter)` matches the `uses=` shorthand."""
    micro = Grelmicro()
    micro.use(MemoryCacheAdapter)

    assert isinstance(micro.get("cache"), Cache)


def test_use_lone_provider_auto_registers() -> None:
    """`micro.use(provider)` on an empty app auto-registers served kinds."""
    micro = Grelmicro()
    micro.use(_MemoryProvider())

    assert isinstance(micro.get("cache"), Cache)
    assert isinstance(micro.get("coordination"), Coordination)


def test_use_provider_after_component_is_lifecycle_only() -> None:
    """`use(component)` then `use(provider)` leaves the provider lifecycle-only."""
    micro = Grelmicro()
    micro.use(Cache(MemoryCacheAdapter()))
    micro.use(_MemoryProvider())

    kinds = {component.kind for component in micro.components}
    assert kinds == {"cache"}
