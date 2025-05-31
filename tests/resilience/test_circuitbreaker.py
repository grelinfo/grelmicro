"""Test CircuitBreaker implementation."""

from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from time import monotonic
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


class SentinelError(Exception):
    """A sentinel error for testing purposes."""


sentinel_error = SentinelError("Sentinel error for testing")


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Clean the circuit breaker registry before and after each test."""
    CircuitBreakerRegistry.clear()
    yield
    CircuitBreakerRegistry.clear()


FrozenTimeType = Union[
    "StepTickTimeFactory", "TickingDateTimeFactory", "FrozenDateTimeFactory"
]


@pytest.fixture(autouse=True)
def frozen_time() -> Iterator[FrozenTimeType]:
    """Freeze time for the duration of the test."""
    with freeze_time("2025-05-27T07:20:55.171802+00:00") as frozen:
        yield frozen


@pytest.fixture
async def circuit_open() -> CircuitBreaker:
    """Fixture for a circuit breaker in the OPEN state."""
    cb = CircuitBreaker("open_circuit")
    cb.force_open()
    return cb


@pytest.fixture
async def circuit_half_open() -> CircuitBreaker:
    """Fixture for a circuit breaker in the HALF_OPEN state."""
    cb = CircuitBreaker("half_open_circuit")
    cb.force_half_open()
    return cb


@pytest.fixture
async def circuit_closed() -> CircuitBreaker:
    """Fixture for a circuit breaker in the CLOSED state."""
    cb = CircuitBreaker("closed_circuit")
    cb.force_closed()
    return cb


def test_circuit_creation() -> None:
    """Test creating a circuit breaker."""
    # Act
    cb = CircuitBreaker("test")
    # Assert
    assert cb.name == "test"


def test_circuit_singleton() -> None:
    """Test circuit breakers are singletons by name."""
    # Arrange
    cb1 = CircuitBreaker("test")
    # Act
    cb2 = CircuitBreaker("test")
    # Assert
    assert cb1 is cb2


def test_registry_getters() -> None:
    """Test circuit breaker registry getters."""
    # Arrange
    cb1 = CircuitBreaker("cb1")
    cb2 = CircuitBreaker("cb2")
    # Act
    all_cb = CircuitBreakerRegistry.get_all()
    get_cb1 = CircuitBreakerRegistry.get("cb1")
    get_none = CircuitBreakerRegistry.get("non-existent")
    # Assert
    assert all_cb == [cb1, cb2]
    assert get_cb1 is cb1
    assert get_none is None


def test_circuit_initial_state() -> None:
    """Test circuit breaker initial state."""
    # Arrange
    cb = CircuitBreaker("test")
    # Assert
    assert cb.current_state is CircuitBreakerState.CLOSED
    assert cb.last_error is None


@pytest.mark.anyio
async def test_circuit_transition_to_open() -> None:
    """Test circuit breaker opens after threshold errors."""
    # Arrange
    cb = CircuitBreaker("test")
    assert cb.current_state == CircuitBreakerState.CLOSED
    # Act
    for _ in range(cb.error_threshold):
        with suppress(SentinelError):
            async with cb.guard():
                raise sentinel_error
    # Assert
    assert cb.current_state == CircuitBreakerState.OPEN


@pytest.mark.anyio
async def test_circuit_open_raises_circuit_breaker_error(
    circuit_open: CircuitBreaker,
) -> None:
    """Test circuit breaker raises CircuitBreakerError when open."""
    # Arrange
    reached = False
    # Act & Assert
    with pytest.raises(CircuitBreakerError):
        async with circuit_open.guard():
            pass
    assert not reached


def test_circuit_breaker_error() -> None:
    """Test CircuitBreakerError."""
    # Arrange
    error_info = ErrorInfo(time=datetime.now(tz=UTC), error=sentinel_error)
    # Act
    error = CircuitBreakerError(
        last_error_info=error_info,
    )
    # Assert
    assert str(error) == (
        "Circuit breaker error: calls not permitted. "
        f"Last error: {type(error_info.error).__name__} at {error_info.time.isoformat()}"
    )
    assert error.last_error_info == error_info


@pytest.mark.anyio
async def test_circuit_transition_to_half_open_on_call(
    frozen_time: FrozenTimeType,
) -> None:
    """Test circuit breaker transitions to half-open after delay."""
    # Arrange
    cb = CircuitBreaker("test_half_open_transition")
    cb._state = (
        CircuitBreakerState.OPEN
    )  # Set the state directly without forcing
    cb.success_threshold = 2  # Avoid immediate closure
    cb._open_until_time = monotonic()  # Set to current time

    # Act
    frozen_time.tick(timedelta(seconds=cb.reset_timeout, microseconds=1))
    async with cb.guard():
        pass
    # Assert
    assert cb.current_state is CircuitBreakerState.HALF_OPEN


@pytest.mark.anyio
async def test_circuit_transition_to_half_open_on_get_state(
    frozen_time: FrozenTimeType,
) -> None:
    """Test circuit breaker transitions to half-open on get state."""
    # Arrange
    cb = CircuitBreaker("test_half_open_get_state")
    cb._state = (
        CircuitBreakerState.OPEN
    )  # Set the state directly without forcing
    cb._open_until_time = monotonic()  # Set to current time

    # Act
    frozen_time.tick(timedelta(seconds=cb.reset_timeout, microseconds=1))
    state = cb.current_state
    # Assert
    assert state is CircuitBreakerState.HALF_OPEN


@pytest.mark.anyio
async def test_circuit_not_transition_to_half_open_on_call(
    frozen_time: FrozenTimeType,
) -> None:
    """Test circuit breaker don't transition before delay."""
    # Arrange
    cb = CircuitBreaker("test_no_half_open_transition")
    cb._state = CircuitBreakerState.OPEN  # Direct state manipulation
    cb._open_until_time = (
        monotonic() + 60
    )  # Set to future time (delay not elapsed)

    # Act
    frozen_time.tick(
        timedelta(seconds=cb.reset_timeout - 1)
    )  # Not enough time elapsed
    with pytest.raises(CircuitBreakerError):
        async with cb.guard():
            pass
    # Assert
    assert cb.current_state is CircuitBreakerState.OPEN


@pytest.mark.anyio
async def test_circuit_not_transition_to_half_open_on_get_state(
    frozen_time: FrozenTimeType,
) -> None:
    """Test circuit breaker don't transition before delay on get state."""
    # Arrange
    cb = CircuitBreaker("test_no_half_open_get_state")
    cb._state = CircuitBreakerState.OPEN  # Direct state manipulation
    cb._open_until_time = monotonic() + 60  # Set to future time

    # Act
    frozen_time.tick(
        timedelta(seconds=cb.reset_timeout - 1)
    )  # Not enough time elapsed
    state = cb.current_state
    # Assert
    assert state is CircuitBreakerState.OPEN


@pytest.mark.anyio
async def test_circuit_half_open_raise_circuit_error(
    circuit_half_open: CircuitBreaker,
) -> None:
    """Test circuit breaker raises error when half-open and no success."""
    # Arrange
    circuit_half_open.half_open_max_concurrency = 1
    # Act
    async with circuit_half_open.guard():
        with pytest.raises(CircuitBreakerError):
            async with circuit_half_open.guard():
                pass


@pytest.mark.anyio
async def test_circuit_transition_to_closed(
    circuit_half_open: CircuitBreaker,
) -> None:
    """Test circuit breaker closes after success threshold in half-open."""
    circuit_half_open.success_threshold = 1
    # Act
    async with circuit_half_open.guard():
        pass
    # Assert
    assert circuit_half_open.current_state is CircuitBreakerState.CLOSED


@pytest.mark.anyio
async def test_circuit_transition_from_half_open_to_open(
    circuit_half_open: CircuitBreaker,
) -> None:
    """Test circuit breaker transitions to open after failure in half-open."""
    circuit_half_open.error_threshold = 1
    # Act
    with suppress(SentinelError):
        async with circuit_half_open.guard():
            raise sentinel_error
    # Assert
    assert circuit_half_open.current_state is CircuitBreakerState.OPEN


@pytest.mark.anyio
async def test_circuit_ignore_error() -> None:
    """Test circuit breaker ignores error."""
    # Arrange
    cb = CircuitBreaker(name="test")
    cb.error_threshold = 1
    # Act
    with pytest.raises(SentinelError, match=f"{sentinel_error}"):
        async with cb.guard(ignore_errors=SentinelError):
            raise sentinel_error
    # Assert
    assert cb.current_state is CircuitBreakerState.CLOSED


@pytest.mark.anyio
async def test_circuit_ignore_errors() -> None:
    """Test circuit breaker ignores errors."""
    # Arrange
    cb = CircuitBreaker(name="test")
    cb.error_threshold = 1
    # Act & Assert
    with pytest.raises(SentinelError):
        async with cb.guard(ignore_errors=(SentinelError, RuntimeError)):
            raise sentinel_error
    with pytest.raises(RuntimeError):
        async with cb.guard(ignore_errors=(ValueError, RuntimeError)):
            raise RuntimeError
    assert cb.current_state is CircuitBreakerState.CLOSED


@pytest.mark.anyio
async def test_circuit_breaker_last_error() -> None:
    """Test error info is properly recorded."""
    # Arrange
    cb = CircuitBreaker(name="test", error_threshold=1)
    # Act
    with suppress(SentinelError):
        async with cb.guard():
            raise sentinel_error
    # Assert
    assert cb._last_error_info == ErrorInfo(
        time=datetime.now(tz=UTC),
        error=sentinel_error,
    )


def test_circuit_statistics_initial() -> None:
    """Test statistics reflect circuit breaker state."""
    # Arrange
    cb = CircuitBreaker(name="test")

    # Act
    stats = cb.statistics()

    # Assert
    assert stats == CircuitBreakerStatistics(
        name="test",
        state=CircuitBreakerState.CLOSED,
        active_calls=0,
        total_error_count=0,
        total_success_count=0,
        creation_time=cb._creation_time,
        consecutive_error_count=0,
        consecutive_success_count=0,
        last_state_change_time=cb._last_state_change_time,
        last_error_info=None,
        last_consecutive_counts_cleared_at=cb._last_consecutive_counts_cleared_at,
    )


@pytest.mark.anyio
async def test_circuit_statistics_with_errors() -> None:
    """Test statistics after errors."""
    # Arrange
    cb = CircuitBreaker(name="test", error_threshold=2)
    error_count = 2
    for i in range(error_count):
        with suppress(RuntimeError, SentinelError):
            async with cb.guard():
                if i == 0:
                    raise RuntimeError
                raise sentinel_error

    # Act
    stats = cb.statistics()

    # Assert
    assert stats.consecutive_error_count == error_count
    assert stats.consecutive_success_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
    ],
)
async def test_circuit_statistics_with_successes(
    state: CircuitBreakerState,
    # circuit_breaker_with_state: callable, # No longer using fixture directly
) -> None:
    """Test statistics in half-open state."""
    # Arrange
    cb = CircuitBreaker(name=f"success_test_cb_{state}")

    cb.success_threshold = 3
    success_count = 3
    for _ in range(success_count):
        async with cb.guard():
            pass
    # Act
    stats = cb.statistics()
    # Assert
    assert stats.consecutive_success_count == success_count


@pytest.mark.anyio
async def test_circuit_statistics_with_successes_half_open() -> None:
    """Test statistics with successes in half-open state."""
    # Arrange
    cb = CircuitBreaker(name="success_test_cb_half_open")
    # Setup HALF_OPEN state directly
    cb._state = CircuitBreakerState.HALF_OPEN

    cb.success_threshold = 3
    success_count = 3
    for _ in range(success_count):
        async with cb.guard():
            pass
    # Act
    stats = cb.statistics()
    # Assert
    assert stats.consecutive_success_count == success_count


def create_circuit_breaker_in_state(
    state: CircuitBreakerState, name: str = "cb"
) -> CircuitBreaker:
    """Create a circuit breaker in the specified state.

    Args:
        state: The desired state for the circuit breaker.
        name: Name of the circuit breaker.

    Returns:
        CircuitBreaker: A circuit breaker instance in the specified state.
    """
    cb = CircuitBreaker(name)
    if state == CircuitBreakerState.OPEN:
        cb.force_open()
    elif state == CircuitBreakerState.HALF_OPEN:
        cb.force_half_open()
    else:
        cb.force_closed()
    return cb


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state,expected_state",
    [
        (CircuitBreakerState.OPEN, CircuitBreakerState.FORCED_OPEN),
        (CircuitBreakerState.CLOSED, CircuitBreakerState.FORCED_CLOSED),
        (CircuitBreakerState.HALF_OPEN, CircuitBreakerState.HALF_OPEN),
    ],
)
async def test_circuit_statistics_state_transition(
    state: CircuitBreakerState,
    expected_state: CircuitBreakerState,
) -> None:
    """Test statistics reflect state transitions."""
    # Arrange
    cb = create_circuit_breaker_in_state(
        state, name=f"transition_test_cb_{state}"
    )
    # Act
    stats = cb.statistics()
    # Assert
    assert stats.state is expected_state
