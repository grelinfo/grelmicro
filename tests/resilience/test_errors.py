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
        "ApiShieldConfig",
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
        "Fallback",
        "FallbackConfig",
        "FallbackResult",
        "FibonacciBackoff",
        "InternalShieldConfig",
        "LinearBackoff",
        "Match",
        "Matcher",
        "MemoryCircuitBreakerAdapter",
        "MemoryRateLimiterAdapter",
        "MemoryTokenBucket",
        "Outcome",
        "PostgresRateLimiterAdapter",
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
        "Shield",
        "ShieldConfig",
        "SlidingWindowConfig",
        "SlowShieldConfig",
        "Timeout",
        "TimeoutConfig",
        "TokenBucketConfig",
        "fallback",
        "falling_back",
        "retry",
        "retrying",
        "shield",
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


def test_resilience_lazy_table_matches_all_and_is_loadable() -> None:
    """Every `_LAZY` key resolves to a real attribute and is listed in `__all__`.

    Guards against a stale lazy table: if a Pattern or algorithm config is
    renamed or removed without updating `_LAZY` or `__all__`, this test
    fails before the broken state ships.
    """
    lazy_keys = set(resilience_mod._LAZY)
    all_set = set(resilience_mod.__all__)
    # Every lazy entry must be advertised on `__all__`.
    assert lazy_keys <= all_set, lazy_keys - all_set
    # Every lazy entry must actually resolve at runtime.
    for name in lazy_keys:
        assert getattr(resilience_mod, name) is not None
