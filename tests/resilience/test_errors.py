"""Test Resilience Errors."""

from datetime import UTC, datetime

import pytest

import grelmicro.resilience as resilience_mod
import grelmicro.resilience.circuitbreaker as cb_mod
import grelmicro.resilience.ratelimiter as rl_mod
from grelmicro.resilience.errors import CircuitBreakerError


def test_circuit_breaker_error() -> None:
    """Test CircuitBreakerError."""
    # Arrange
    time = datetime.now(tz=UTC)
    exc = Exception("This is a test error")

    # Act
    error = CircuitBreakerError(
        name="test",
        last_error_time=time,
        last_error=exc,
    )

    # Assert
    assert str(error) == "Circuit breaker 'test' call not permitted"
    assert error.last_error == exc
    assert error.last_error_time == time


def test_resilience_module_exports() -> None:
    """Test resilience module __all__ contains expected symbols."""
    expected = {
        "CircuitBreakers",
        "CircuitBreaker",
        "CircuitBreakerBackend",
        "CircuitBreakerConfig",
        "CircuitBreakerError",
        "CircuitBreakerMetrics",
        "CircuitBreakerSnapshot",
        "CircuitBreakerState",
        "CircuitBreakerStrategy",
        "ConsecutiveCountConfig",
        "ConstantBackoff",
        "ErrorDetails",
        "ExponentialBackoff",
        "FibonacciBackoff",
        "LinearBackoff",
        "Match",
        "Matcher",
        "MemoryCircuitBreakerAdapter",
        "MemoryRateLimiterAdapter",
        "MemoryTokenBucket",
        "Outcome",
        "RandomBackoff",
        "RateLimiters",
        "RateLimitExceededError",
        "RateLimitResult",
        "RateLimiter",
        "RateLimiterBackend",
        "RateLimiterConfig",
        "RateLimiterStrategy",
        "RedisCircuitBreakerAdapter",
        "RedisRateLimiterAdapter",
        "ResilienceError",
        "ResilienceSettingsValidationError",
        "Retry",
        "RetryAttempt",
        "RetryBackoffConfig",
        "RetryConfig",
        "RetryStrategy",
        "SlidingWindowConfig",
        "TokenBucketConfig",
        "retry",
        "retrying",
    }
    assert set(resilience_mod.__all__) == expected


def test_resilience_lazy_loader_unknown_attribute_raises() -> None:
    """The top-level lazy loader raises `AttributeError` for unknown names."""
    with pytest.raises(AttributeError, match=r"grelmicro\.resilience"):
        _ = resilience_mod.NotAThing  # type: ignore[attr-defined]


def test_circuitbreaker_lazy_loader_unknown_attribute_raises() -> None:
    """The circuit-breaker subpackage loader rejects unknown names."""
    with pytest.raises(AttributeError, match="circuitbreaker"):
        _ = cb_mod.NotAThing  # type: ignore[attr-defined]


def test_ratelimiter_lazy_loader_unknown_attribute_raises() -> None:
    """The rate-limiter subpackage loader rejects unknown names."""
    with pytest.raises(AttributeError, match="ratelimiter"):
        _ = rl_mod.NotAThing  # type: ignore[attr-defined]
