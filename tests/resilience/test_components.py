"""Tests for the `RateLimiterRegistry` and `CircuitBreakerRegistry` Components."""

from __future__ import annotations

from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import CircuitBreakerRegistry, RateLimiterRegistry
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
    """`RateLimiterRegistry(adapter).backend` returns the wrapped adapter."""
    adapter = MemoryRateLimiterAdapter()
    component = RateLimiterRegistry(adapter)
    assert component.backend is adapter
    assert component.name == "default"
    assert component.kind == "ratelimiter"


def test_breaker_exposes_backend() -> None:
    """`CircuitBreakerRegistry(adapter).backend` returns the wrapped adapter."""
    adapter = MemoryCircuitBreakerAdapter()
    component = CircuitBreakerRegistry(adapter)
    assert component.backend is adapter
    assert component.name == "default"
    assert component.kind == "circuitbreaker"


def test_use_auto_wraps_rate_limiter_backend() -> None:
    """`Grelmicro.use(adapter)` auto-wraps a `RateLimiterBackend` in `RateLimiterRegistry`."""
    adapter = MemoryRateLimiterAdapter()
    micro = Grelmicro(uses=[adapter])
    component = micro.get("ratelimiter", "default")
    assert isinstance(component, RateLimiterRegistry)
    assert component.backend is adapter


def test_use_auto_wraps_circuit_breaker_backend() -> None:
    """`Grelmicro.use(adapter)` auto-wraps a `CircuitBreakerBackend` in `CircuitBreakerRegistry`."""
    adapter = MemoryCircuitBreakerAdapter()
    micro = Grelmicro(uses=[adapter])
    component = micro.get("circuitbreaker", "default")
    assert isinstance(component, CircuitBreakerRegistry)
    assert component.backend is adapter


async def test_ratelimit_lifecycles_backend() -> None:
    """`RateLimiterRegistry` opens and closes the wrapped backend as a context manager."""
    adapter = MemoryRateLimiterAdapter()
    async with RateLimiterRegistry(adapter):
        pass


async def test_breaker_lifecycles_backend() -> None:
    """`CircuitBreakerRegistry` opens and closes the wrapped backend as a context manager."""
    adapter = MemoryCircuitBreakerAdapter()
    async with CircuitBreakerRegistry(adapter):
        pass


def test_ratelimit_accepts_redis_provider() -> None:
    """`RateLimiterRegistry(RedisProvider(...))` calls `provider.ratelimiter()`."""
    provider = RedisProvider("redis://localhost:6379/0")
    component = RateLimiterRegistry(provider)
    assert isinstance(component.backend, RedisRateLimiterAdapter)
    assert component.backend.provider is provider


def test_ratelimit_accepts_postgres_provider() -> None:
    """`RateLimiterRegistry(PostgresProvider(...))` calls `provider.ratelimiter()`."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    component = RateLimiterRegistry(provider)
    assert isinstance(component.backend, PostgresRateLimiterAdapter)
    assert component.backend.provider is provider


def test_ratelimit_accepts_sqlite_provider() -> None:
    """`RateLimiterRegistry(SQLiteProvider(...))` calls `provider.ratelimiter()`."""
    provider = SQLiteProvider("app.db")
    component = RateLimiterRegistry(provider)
    assert isinstance(component.backend, SQLiteRateLimiterAdapter)
    assert component.backend.provider is provider


def test_breaker_with_postgres_provider_builds_shared_adapter() -> None:
    """`CircuitBreakerRegistry(PostgresProvider(...))` resolves to the Postgres adapter."""
    provider = PostgresProvider("postgresql://localhost:5432/app")
    component = CircuitBreakerRegistry(provider)
    assert isinstance(component.backend, PostgresCircuitBreakerAdapter)
    assert component.backend.provider is provider


def test_breaker_with_redis_provider_builds_shared_adapter() -> None:
    """`CircuitBreakerRegistry(RedisProvider(...))` resolves to the matching Redis adapter."""
    provider = RedisProvider("redis://localhost:6379/0")
    component = CircuitBreakerRegistry(provider)
    assert isinstance(component.backend, RedisCircuitBreakerAdapter)
    assert component.backend.is_shared is True
