"""Test Resilience Errors."""

from datetime import UTC, datetime

from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerMetrics,
    CircuitBreakerState,
    ErrorDetails,
)
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
    """Test resilience module exports all expected symbols."""
    # Assert
    assert CircuitBreaker is not None
    assert CircuitBreakerError is not None
    assert CircuitBreakerMetrics is not None
    assert CircuitBreakerState is not None
    assert ErrorDetails is not None
