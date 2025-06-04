"""Test Resilience Errors."""

from datetime import UTC, datetime

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
