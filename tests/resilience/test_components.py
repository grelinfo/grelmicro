"""Tests for the `RateLimiters` and `CircuitBreakers` Components."""

from __future__ import annotations

import pytest

from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreakers, RateLimiters
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.circuitbreaker.redis import (
    RedisCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.resilience.ratelimiter.postgres import PostgresRateLimiterAdapter
from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter


def test_ratelimit_exposes_backend() -> None:
    """`RateLimiters(adapter).backend` returns the wrapped adapter."""
    adapter = MemoryRateLimiterAdapter()
    component = RateLimiters(adapter)
    assert component.backend is adapter
    assert component.name == "default"
    assert component.kind == "ratelimiter"


def test_breaker_exposes_backend() -> None:
    """`CircuitBreakers(adapter).backend` returns the wrapped adapter."""
    adapter = MemoryCircuitBreakerAdapter()
    component = CircuitBreakers(adapter)
    assert component.backend is adapter
    assert component.name == "default"
    assert component.kind == "circuitbreaker"


def test_use_auto_wraps_rate_limiter_backend() -> None:
    """`Grelmicro.use(adapter)` auto-wraps a `RateLimiterBackend` in `RateLimiters`."""
    adapter = MemoryRateLimiterAdapter()
    micro = Grelmicro(uses=[adapter])
    component = micro.get("ratelimiter", "default")
    assert isinstance(component, RateLimiters)
    assert component.backend is adapter


def test_use_auto_wraps_circuit_breaker_backend() -> None:
    """`Grelmicro.use(adapter)` auto-wraps a `CircuitBreakerBackend` in `CircuitBreakers`."""
    adapter = MemoryCircuitBreakerAdapter()
    micro = Grelmicro(uses=[adapter])
    component = micro.get("circuitbreaker", "default")
    assert isinstance(component, CircuitBreakers)
    assert component.backend is adapter


async def test_ratelimit_lifecycles_backend() -> None:
    """`RateLimiters` opens and closes the wrapped backend as a context manager."""
    adapter = MemoryRateLimiterAdapter()
    async with RateLimiters(adapter):
        pass


async def test_breaker_lifecycles_backend() -> None:
    """`CircuitBreakers` opens and closes the wrapped backend as a context manager."""
    adapter = MemoryCircuitBreakerAdapter()
    async with CircuitBreakers(adapter):
        pass


def test_ratelimit_accepts_redis_provider() -> None:
    """`RateLimiters(RedisProvider(...))` calls `provider.ratelimiter()` to build the adapter."""
    provider = RedisProvider("redis://localhost:6379/0")
    component = RateLimiters(provider)
    assert isinstance(component.backend, RedisRateLimiterAdapter)
    assert component.backend.provider is provider


def test_ratelimit_accepts_postgres_provider() -> None:
    """`RateLimiters(PostgresProvider(...))` calls `provider.ratelimiter()` to build the adapter."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    component = RateLimiters(provider)
    assert isinstance(component.backend, PostgresRateLimiterAdapter)
    assert component.backend.provider is provider


def test_breaker_with_postgres_provider_raises() -> None:
    """`CircuitBreakers(PostgresProvider(...))` raises `NotImplementedError`."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    with pytest.raises(NotImplementedError, match="no circuit breaker adapter"):
        CircuitBreakers(provider)


def test_breaker_with_redis_provider_builds_shared_adapter() -> None:
    """`CircuitBreakers(RedisProvider(...))` resolves to the canonical Redis adapter."""
    provider = RedisProvider("redis://localhost:6379/0")
    component = CircuitBreakers(provider)
    assert isinstance(component.backend, RedisCircuitBreakerAdapter)
    assert component.backend.is_shared is True
