"""Test Resilience Errors."""

from datetime import UTC, datetime

import grelmicro.resilience as resilience_mod
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
        "CircuitBreaker",
        "CircuitBreakerConfig",
        "CircuitBreakerError",
        "CircuitBreakerMetrics",
        "CircuitBreakerState",
        "ErrorDetails",
        "GCRAConfig",
        "MemoryTokenBucket",
        "RateLimitExceededError",
        "RateLimitResult",
        "RateLimiter",
        "RateLimiterBackend",
        "RateLimiterConfig",
        "RateLimiterStrategy",
        "ResilienceError",
        "ResilienceSettingsValidationError",
        "TokenBucketConfig",
        "register",
        "unregister",
        "use",
        "use_backend",
    }
    assert set(resilience_mod.__all__) == expected
