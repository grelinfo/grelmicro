"""Tests for the `RateLimiterComponent` and `CircuitBreakerComponent` Components."""

from __future__ import annotations

from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import CircuitBreakerComponent, RateLimiterComponent
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.circuitbreaker.postgres import (
    PostgresCircuitBreakerAdapter,
)
from grelmicro.resilience.circuitbreaker.redis import (
    RedisCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.resilience.ratelimiter.postgres import PostgresRateLimiterAdapter
from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter
from grelmicro.resilience.ratelimiter.sqlite import SQLiteRateLimiterAdapter


def test_ratelimit_exposes_backend() -> None:
    """`RateLimiterComponent(adapter).backend` returns the wrapped adapter."""
    adapter = MemoryRateLimiterAdapter()
    component = RateLimiterComponent(adapter)
    assert component.backend is adapter
    assert component.name == "default"
    assert component.kind == "ratelimiter"


def test_breaker_exposes_backend() -> None:
    """`CircuitBreakerComponent(adapter).backend` returns the wrapped adapter."""
    adapter = MemoryCircuitBreakerAdapter()
    component = CircuitBreakerComponent(adapter)
    assert component.backend is adapter
    assert component.name == "default"
    assert component.kind == "circuitbreaker"


def test_use_auto_wraps_rate_limiter_backend() -> None:
    """`Grelmicro.use(adapter)` auto-wraps a `RateLimiterBackend` in `RateLimiterComponent`."""
    adapter = MemoryRateLimiterAdapter()
    micro = Grelmicro(uses=[adapter])
    component = micro.get("ratelimiter", "default")
    assert isinstance(component, RateLimiterComponent)
    assert component.backend is adapter


def test_use_auto_wraps_circuit_breaker_backend() -> None:
    """`Grelmicro.use(adapter)` auto-wraps a `CircuitBreakerBackend`."""
    adapter = MemoryCircuitBreakerAdapter()
    micro = Grelmicro(uses=[adapter])
    component = micro.get("circuitbreaker", "default")
    assert isinstance(component, CircuitBreakerComponent)
    assert component.backend is adapter


async def test_ratelimit_lifecycles_backend() -> None:
    """`RateLimiterComponent` opens and closes the wrapped backend as a context manager."""
    adapter = MemoryRateLimiterAdapter()
    async with RateLimiterComponent(adapter):
        pass


async def test_breaker_lifecycles_backend() -> None:
    """`CircuitBreakerComponent` opens and closes the wrapped backend as a context manager."""
    adapter = MemoryCircuitBreakerAdapter()
    async with CircuitBreakerComponent(adapter):
        pass


def test_ratelimit_accepts_redis_provider() -> None:
    """`RateLimiterComponent(RedisProvider(...))` calls `provider.ratelimiter()`."""
    provider = RedisProvider("redis://localhost:6379/0")
    component = RateLimiterComponent(provider)
    assert isinstance(component.backend, RedisRateLimiterAdapter)
    assert component.backend.provider is provider


def test_ratelimit_accepts_postgres_provider() -> None:
    """`RateLimiterComponent(PostgresProvider(...))` calls `provider.ratelimiter()`."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    component = RateLimiterComponent(provider)
    assert isinstance(component.backend, PostgresRateLimiterAdapter)
    assert component.backend.provider is provider


def test_ratelimit_accepts_sqlite_provider() -> None:
    """`RateLimiterComponent(SQLiteProvider(...))` calls `provider.ratelimiter()`."""
    provider = SQLiteProvider("app.db")
    component = RateLimiterComponent(provider)
    assert isinstance(component.backend, SQLiteRateLimiterAdapter)
    assert component.backend.provider is provider


def test_breaker_with_postgres_provider_builds_shared_adapter() -> None:
    """`CircuitBreakerComponent(PostgresProvider(...))` resolves to the Postgres adapter."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    component = CircuitBreakerComponent(provider)
    assert isinstance(component.backend, PostgresCircuitBreakerAdapter)
    assert component.backend.provider is provider


def test_breaker_with_redis_provider_builds_shared_adapter() -> None:
    """`CircuitBreakerComponent(RedisProvider(...))` resolves to the matching Redis adapter."""
    provider = RedisProvider("redis://localhost:6379/0")
    component = CircuitBreakerComponent(provider)
    assert isinstance(component.backend, RedisCircuitBreakerAdapter)
    assert component.backend.is_shared is True
