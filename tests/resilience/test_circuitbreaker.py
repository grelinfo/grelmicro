"""Test CircuitBreaker implementation."""

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


async def circuit_with_state(
    frozen_time: FrozenTimeType, state: CircuitBreakerState
) -> CircuitBreaker:
    """Return a circuit breaker in the requested state.

    Args:
        frozen_time: The frozen time fixture
        state: The desired circuit breaker state

    Returns:
        A circuit breaker in the specified state
    """
    match state:
        case CircuitBreakerState.CLOSED:
            cb = CircuitBreaker("test_circuit")

        case CircuitBreakerState.OPEN:
            cb = CircuitBreaker("test_circuit")
            cb.half_open_max_duration = 100
            for _ in range(cb.error_threshold):
                with suppress(SentinelError):
                    async with cb.guard():
                        raise SentinelError
            assert cb.state is CircuitBreakerState.OPEN

        case CircuitBreakerState.HALF_OPEN:
            # First create an OPEN circuit breaker
            cb = CircuitBreaker("test_circuit")
            cb.half_open_max_duration = 100
            cb.success_threshold = 2  # Avoid immediate closure
            for _ in range(cb.error_threshold):
                with suppress(SentinelError):
                    async with cb.guard():
                        raise SentinelError
            assert cb.state is CircuitBreakerState.OPEN

            # Then transition to HALF_OPEN
            frozen_time.tick(
                timedelta(seconds=cb.half_open_max_duration, microseconds=1)
            )
            assert cb.state is CircuitBreakerState.HALF_OPEN
    return cb


@pytest.fixture
async def circuit_open(frozen_time: FrozenTimeType) -> CircuitBreaker:
    """Fixture for a circuit breaker in the OPEN state."""
    cb = await circuit_with_state(frozen_time, CircuitBreakerState.OPEN)
    return cb


@pytest.fixture
async def circuit_half_open(frozen_time: FrozenTimeType) -> CircuitBreaker:
    """Fixture for a circuit breaker in the HALF_OPEN state."""
    cb = await circuit_with_state(frozen_time, CircuitBreakerState.HALF_OPEN)
    return cb


@pytest.fixture
async def circuit_closed(frozen_time: FrozenTimeType) -> CircuitBreaker:
    """Fixture for a circuit breaker in the CLOSED state."""
    cb = await circuit_with_state(frozen_time, CircuitBreakerState.CLOSED)
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
    assert cb.state is CircuitBreakerState.CLOSED
    assert cb.last_error is None


@pytest.mark.anyio
async def test_circuit_transition_to_open() -> None:
    """Test circuit breaker opens after threshold errors."""
    # Arrange
    cb = CircuitBreaker("test")
    assert cb.state == CircuitBreakerState.CLOSED
    # Act
    for _ in range(cb.error_threshold):
        with suppress(SentinelError):
            async with cb.guard():
                raise sentinel_error
    # Assert
    assert cb.state == CircuitBreakerState.OPEN


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
    error_info = ErrorInfo(timestamp=datetime.now(tz=UTC), error=sentinel_error)
    # Act
    error = CircuitBreakerError(
        last_error=error_info,
    )
    # Assert
    assert str(error) == (
        "Circuit breaker error: calls not permitted"
        " [last_error_type=SentinelError, last_error_time=2025-05-27T07:20:55.171802+00:00]"
    )
    assert error.last_error == error_info


@pytest.mark.anyio
async def test_circuit_transition_to_half_open_on_call(
    circuit_open: CircuitBreaker, frozen_time: FrozenTimeType
) -> None:
    """Test circuit breaker transitions to half-open after delay."""
    # Arrange
    circuit_open.success_threshold = 2  # Avoid immediate closure
    # Act
    frozen_time.tick(
        timedelta(seconds=circuit_open.half_open_max_duration, microseconds=1)
    )
    async with circuit_open.guard():
        pass
    # Assert
    assert circuit_open.state is CircuitBreakerState.HALF_OPEN


@pytest.mark.anyio
async def test_circuit_transition_to_half_open_on_get_state(
    circuit_open: CircuitBreaker, frozen_time: FrozenTimeType
) -> None:
    """Test circuit breaker transitions to half-open on get state."""
    # Arrange
    frozen_time.tick(
        timedelta(seconds=circuit_open.half_open_max_duration, microseconds=1)
    )
    # Act
    state = circuit_open.state
    # Assert
    assert state is CircuitBreakerState.HALF_OPEN


@pytest.mark.anyio
async def test_circuit_not_transition_to_half_open_on_call(
    circuit_open: CircuitBreaker, frozen_time: FrozenTimeType
) -> None:
    """Test circuit breaker don't transition before delay."""
    # Act
    frozen_time.tick(timedelta(seconds=circuit_open.half_open_max_duration))
    with pytest.raises(CircuitBreakerError):
        async with circuit_open.guard():
            pass
    # Assert
    assert circuit_open.state is CircuitBreakerState.OPEN


@pytest.mark.anyio
async def test_circuit_not_transition_to_half_open_on_get_state(
    circuit_open: CircuitBreaker, frozen_time: FrozenTimeType
) -> None:
    """Test circuit breaker don't transition before delay on get state."""
    # Act
    frozen_time.tick(timedelta(seconds=circuit_open.half_open_max_duration))
    state = circuit_open.state
    # Assert
    assert state is CircuitBreakerState.OPEN


@pytest.mark.anyio
async def test_circuit_half_open_raise_circuit_error(
    circuit_half_open: CircuitBreaker,
) -> None:
    """Test circuit breaker raises error when half-open and no success."""
    # Arrange
    circuit_half_open.half_open_concurrent_calls = 1
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
    assert circuit_half_open.state is CircuitBreakerState.CLOSED


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
    assert circuit_half_open.state is CircuitBreakerState.OPEN


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
    assert cb.state is CircuitBreakerState.CLOSED


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
    assert cb.state is CircuitBreakerState.CLOSED


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
    assert cb.last_error == ErrorInfo(
        timestamp=datetime.now(tz=UTC),
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
        error_count=0,
        success_count=0,
        last_error=None,
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
    assert stats.error_count == error_count
    assert stats.success_count == 0


@pytest.mark.anyio
async def test_circuit_statistics_with_successes(
    circuit_half_open: CircuitBreaker,
) -> None:
    """Test statistics in half-open state."""
    # Arrange
    circuit_half_open.success_threshold = 3
    success_count = 3

    for _ in range(success_count):
        async with circuit_half_open.guard():
            pass

    # Act
    stats = circuit_half_open.statistics()

    # Assert
    assert stats.error_count == 0
    assert stats.success_count == success_count


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.OPEN,
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
    ],
)
async def test_circuit_statistics_state_transition(
    frozen_time: FrozenTimeType, state: CircuitBreakerState
) -> None:
    """Test statistics reflect state transitions."""
    cb = await circuit_with_state(frozen_time, state)
    stats = cb.statistics()
    # Assert
    assert stats.state is state
