"""Test CircuitBreaker implementation."""

import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Union

import pytest
from freezegun import freeze_time

if TYPE_CHECKING:
    from freezegun.api import (
        FrozenDateTimeFactory,
        StepTickTimeFactory,
        TickingDateTimeFactory,
    )

from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitBreakerRegistry,
    CircuitBreakerState,
    CircuitBreakerStatistics,
    ErrorInfo,
)


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Clean the circuit breaker registry before and after each test."""
    CircuitBreakerRegistry.clear()
    yield
    CircuitBreakerRegistry.clear()


FrozenTimeType = Union[
    "StepTickTimeFactory", "TickingDateTimeFactory", "FrozenDateTimeFactory"
]


@pytest.fixture
def frozen_time() -> Iterator[FrozenTimeType]:
    """Freeze time for the duration of the test."""
    with freeze_time() as frozen:
        yield frozen


def test_circuit_breaker_creation() -> None:
    """Test creating a circuit breaker."""
    # Act
    cb = CircuitBreaker("test")

    # Assert
    assert cb.name == "test"


def test_circuit_breaker_singleton() -> None:
    """Test circuit breakers are singletons by name."""
    # Arrange
    cb1 = CircuitBreaker("test")

    # Act
    cb2 = CircuitBreaker("test")

    # Assert
    assert cb1 is cb2


def test_registry_operations() -> None:
    """Test circuit breaker registry operations."""
    # Arrange
    cb1 = CircuitBreaker("cb1")
    cb2 = CircuitBreaker("cb2")

    # Act
    all_cb = CircuitBreakerRegistry.all()
    get_cb1 = CircuitBreakerRegistry.get("cb1")
    get_none = CircuitBreakerRegistry.get("non-existent")

    # Assert
    assert all_cb == [cb1, cb2]
    assert get_cb1 is cb1
    assert get_none is None


# State transition tests
def test_circuit_initial_state() -> None:
    """Test circuit breaker initial state."""
    # Arrange
    cb = CircuitBreaker("test")

    # Assert
    assert cb.state is CircuitBreakerState.CLOSED
    assert cb.last_error is None
    assert cb.statistics() == CircuitBreakerStatistics(
        name="test",
        state=CircuitBreakerState.CLOSED,
        error_count=0,
        success_count=0,
        last_error=None,
    )


def test_circuit_open_after_threshold() -> None:
    """Test circuit breaker opens after threshold errors."""
    # Arrange
    cb = CircuitBreaker("test", error_threshold=3)
    assert cb.state == CircuitBreakerState.CLOSED
    # Act
    for _ in range(3):
        with suppress(ValueError), cb.guard():
            raise ValueError
    # Assert
    assert cb.state == CircuitBreakerState.OPEN


@freeze_time()
def test_circuit_raises_circuit_breaker_error() -> None:
    """Test circuit breaker raises CircuitBreakerError when open."""
    # Arrange
    error = ValueError("test")
    cb = CircuitBreaker("test", error_threshold=1)
    with suppress(ValueError), cb.guard():
        raise error
    assert cb.state == CircuitBreakerState.OPEN
    # Act
    with pytest.raises(CircuitBreakerError) as exc_info, cb.guard():
        pass
    # Assert
    assert exc_info.value.state is CircuitBreakerState.OPEN
    assert exc_info.value.last_error == ErrorInfo(
        timestamp=datetime.now(tz=UTC), error=error
    )


@freeze_time("2025-05-27T07:20:55.171802+00:00")
def test_circuit_breaker_error() -> None:
    """Test CircuitBreakerError."""
    # Arrange
    error_info = ErrorInfo(
        timestamp=datetime.now(tz=UTC), error=ValueError("Test error")
    )
    # Act
    error = CircuitBreakerError(
        state=CircuitBreakerState.OPEN,
        last_error=error_info,
    )
    # Assert
    assert str(error) == (
        "Circuit breaker error: call not permitted in state 'OPEN'"
        " [last_error_type=ValueError, last_error_time=2025-05-27T07:20:55.171802+00:00]"
    )
    assert error.state is CircuitBreakerState.OPEN
    assert error.last_error == error_info


def test_circuit_half_open_after_delay(frozen_time: FrozenTimeType) -> None:
    """Test circuit breaker transitions to half-open after delay."""
    # Arrange
    cb = CircuitBreaker(
        name="test",
        error_threshold=1,
        half_open_delay=1,
    )
    with suppress(ValueError), cb.guard():
        raise ValueError
    assert cb.state == CircuitBreakerState.OPEN

    # Act
    frozen_time.tick(timedelta(seconds=1, microseconds=1))
    with cb.guard():
        state = cb.state

    # Assert
    assert state == CircuitBreakerState.HALF_OPEN


def test_circuit_open_before_delay(frozen_time: FrozenTimeType) -> None:
    """Test circuit breaker don't transition before delay."""
    # Arrange
    cb = CircuitBreaker(
        name="test",
        error_threshold=1,
        half_open_delay=1,
    )
    with suppress(ValueError), cb.guard():
        raise ValueError
    assert cb.state == CircuitBreakerState.OPEN

    # Act
    frozen_time.tick(timedelta(seconds=1))
    with pytest.raises(CircuitBreakerError), cb.guard():
        pass

    # Assert
    assert cb.state == CircuitBreakerState.OPEN


def test_half_open_limited_calls(frozen_time: FrozenTimeType) -> None:
    """Test circuit breaker in half-open state allows limited calls."""
    # Arrange
    concurrent_calls = 3
    cb = CircuitBreaker(
        name="test",
        error_threshold=1,
        half_open_delay=1,
        half_open_concurrent_calls=concurrent_calls,
    )
    with suppress(ValueError), cb.guard():
        raise ValueError
    assert cb.state is CircuitBreakerState.OPEN
    cb.error_threshold = (
        10  # Increase threshold to allow more calls in half-open
    )
    frozen_time.tick(timedelta(seconds=2))

    # Act
    counter = 0
    counter_lock = threading.Lock()

    def guarded_call():
        nonlocal counter
        with suppress(CircuitBreakerError):
            with cb.guard():
                with counter_lock:
                    counter += 1

    threads = []
    for _ in range(concurrent_calls + 1):
        t = threading.Thread(target=guarded_call)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Assert
    assert counter == concurrent_calls


def test_closed_after_success_threshold() -> None:
    """Test circuit breaker closes after success threshold in half-open."""
    # Arrange
    cb = CircuitBreaker(
        name="test",
        error_threshold=1,
        half_open_delay=0.01,
        half_open_success_threshold=2,
    )

    # Act - Generate error to open circuit
    try:
        with cb.guard():
            raise ValueError("Test error")
    except ValueError:
        pass

    time.sleep(0.02)  # Wait for half-open

    # Act - First successful call
    with cb.guard():
        pass

    # Assert - Still half-open after 1 success
    assert cb.state == CircuitBreakerState.HALF_OPEN

    # Act - Second successful call
    with cb.guard():
        pass

    # Assert - Now closed after 2 successes
    assert cb.state == CircuitBreakerState.CLOSED


def test_reopens_on_error_in_half_open() -> None:
    """Test circuit breaker reopens on error in half-open state."""
    # Arrange
    cb = CircuitBreaker(name="test", error_threshold=1, half_open_delay=0.01)

    # Act - Generate error to open circuit
    try:
        with cb.guard():
            raise ValueError("Test error")
    except ValueError:
        pass

    time.sleep(0.02)  # Wait for half-open

    # Act - Generate error in half-open state
    try:
        with cb.guard():
            raise ValueError("Another error")
    except ValueError:
        pass

    # Assert
    assert cb.state == CircuitBreakerState.OPEN


# Error handling functionality tests
def test_exclude_errors() -> None:
    """Test excluded errors don't count toward threshold."""
    # Arrange
    cb = CircuitBreaker(name="test", error_threshold=1)

    # Act - Generate excluded error
    try:
        with cb.guard(exclude_errors=(ValueError,)):
            raise ValueError("Excluded error")
    except ValueError:
        pass

    # Assert - Still closed because error was excluded
    assert cb.state == CircuitBreakerState.CLOSED

    # Act - Generate non-excluded error
    try:
        with cb.guard(exclude_errors=(ValueError,)):
            raise KeyError("Non-excluded error")
    except KeyError:
        pass

    # Assert - Now open
    assert cb.state == CircuitBreakerState.OPEN


def test_error_info_recorded() -> None:
    """Test error info is properly recorded."""
    # Arrange
    cb = CircuitBreaker(name="test", error_threshold=1)
    test_error = ValueError("Test error")

    # Act
    try:
        with cb.guard():
            raise test_error
    except ValueError:
        pass

    # Assert
    assert cb.last_error is not None
    assert cb.last_error.error is test_error
    assert isinstance(cb.last_error.timestamp, datetime)


def test_circuit_breaker_error_str() -> None:
    """Test string representation of CircuitBreakerError."""
    # Arrange
    cb = CircuitBreaker(name="test", error_threshold=1)

    # Act - Generate error to open circuit
    try:
        with cb.guard():
            raise ValueError("Test error")
    except ValueError:
        pass

    # Generate CircuitBreakerError
    error = None
    try:
        with cb.guard():
            pass
    except CircuitBreakerError as e:
        error = e

    # Assert
    error_str = str(error)
    assert "Circuit breaker error: call not permitted" in error_str
    assert "OPEN" in error_str
    assert "ValueError" in error_str


# Statistics tests
def test_statistics() -> None:
    """Test statistics reflect circuit breaker state."""
    # Arrange
    cb = CircuitBreaker(name="test", error_threshold=2)

    # Act - Initial state
    initial_stats = cb.statistics()

    # Act - Generate one error
    try:
        with cb.guard():
            raise ValueError("Test error")
    except ValueError:
        pass

    one_error_stats = cb.statistics()

    # Act - Generate second error
    try:
        with cb.guard():
            raise ValueError("Test error")
    except ValueError:
        pass

    two_errors_stats = cb.statistics()

    # Assert
    assert initial_stats.state == CircuitBreakerState.CLOSED
    assert initial_stats.error_count == 0
    assert initial_stats.success_count == 0
    assert initial_stats.last_error is None

    assert one_error_stats.state == CircuitBreakerState.CLOSED
    assert one_error_stats.error_count == 1
    assert one_error_stats.success_count == 0
    assert one_error_stats.last_error is not None

    assert two_errors_stats.state == CircuitBreakerState.OPEN
    assert two_errors_stats.error_count == 2
    assert two_errors_stats.success_count == 0
    assert two_errors_stats.last_error is not None


# Edge case tests
def test_success_resets_error_count() -> None:
    """Test successful calls reset error count."""
    # Arrange
    cb = CircuitBreaker(name="test", error_threshold=3)

    # Act - Generate two errors
    for _ in range(2):
        try:
            with cb.guard():
                raise ValueError("Test error")
        except ValueError:
            pass

    # Act - One successful call
    with cb.guard():
        pass

    # Act - Generate two more errors
    for _ in range(2):
        try:
            with cb.guard():
                raise ValueError("Test error")
        except ValueError:
            pass

    # Assert - Still closed because error count was reset
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.statistics().error_count == 2
