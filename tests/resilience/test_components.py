"""Tests for the `RateLimit` and `Breaker` Components."""

from __future__ import annotations

import pytest

from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import Breaker, RateLimit
from grelmicro.resilience.memory import (
    MemoryCircuitBreakerAdapter,
    MemoryRateLimiterAdapter,
)
from grelmicro.resilience.redis import RedisRateLimiterAdapter


def test_ratelimit_exposes_backend() -> None:
    """`RateLimit(adapter).backend` returns the wrapped adapter."""
    adapter = MemoryRateLimiterAdapter()
    component = RateLimit(adapter)
    assert component.backend is adapter
    assert component.name == "default"
    assert component.kind == "ratelimiter"


def test_breaker_exposes_backend() -> None:
    """`Breaker(adapter).backend` returns the wrapped adapter."""
    adapter = MemoryCircuitBreakerAdapter()
    component = Breaker(adapter)
    assert component.backend is adapter
    assert component.name == "default"
    assert component.kind == "circuitbreaker"


def test_use_auto_wraps_rate_limiter_backend() -> None:
    """`Grelmicro.use(adapter)` auto-wraps a `RateLimiterBackend` in `RateLimit`."""
    adapter = MemoryRateLimiterAdapter()
    micro = Grelmicro(uses=[adapter])
    component = micro.get("ratelimiter", "default")
    assert isinstance(component, RateLimit)
    assert component.backend is adapter


def test_use_auto_wraps_circuit_breaker_backend() -> None:
    """`Grelmicro.use(adapter)` auto-wraps a `CircuitBreakerBackend` in `Breaker`."""
    adapter = MemoryCircuitBreakerAdapter()
    micro = Grelmicro(uses=[adapter])
    component = micro.get("circuitbreaker", "default")
    assert isinstance(component, Breaker)
    assert component.backend is adapter


async def test_ratelimit_lifecycles_backend() -> None:
    """`RateLimit` opens and closes the wrapped backend as a context manager."""
    adapter = MemoryRateLimiterAdapter()
    async with RateLimit(adapter):
        pass


async def test_breaker_lifecycles_backend() -> None:
    """`Breaker` opens and closes the wrapped backend as a context manager."""
    adapter = MemoryCircuitBreakerAdapter()
    async with Breaker(adapter):
        pass


def test_ratelimit_accepts_redis_provider() -> None:
    """`RateLimit(RedisProvider(...))` calls `provider.ratelimiter()` to build the adapter."""
    provider = RedisProvider("redis://localhost:6379/0")
    component = RateLimit(provider)
    assert isinstance(component.backend, RedisRateLimiterAdapter)
    assert component.backend.provider is provider


def test_ratelimit_with_postgres_provider_raises() -> None:
    """`RateLimit(PostgresProvider(...))` raises `NotImplementedError`."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    with pytest.raises(NotImplementedError, match="no rate limiter adapter"):
        RateLimit(provider)


def test_breaker_with_provider_raises() -> None:
    """`Breaker(provider)` raises `NotImplementedError` (no provider ships a breaker today)."""
    provider = RedisProvider("redis://localhost:6379/0")
    with pytest.raises(NotImplementedError, match="no circuit breaker adapter"):
        Breaker(provider)
