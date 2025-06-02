"""Test CircuitBreaker implementation."""

from collections.abc import Iterator
from contextlib import AsyncExitStack, suppress
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
    CircuitBreakerMetrics,
    CircuitBreakerRegistry,
    CircuitBreakerState,
    ErrorDetails,
)


class SentinelError(Exception):
    """A sentinel error for testing purposes."""


sentinel_error = SentinelError("Sentinel error for testing")


def create_circuit_breaker_in_state(
    state: CircuitBreakerState,
) -> CircuitBreaker:
    """Create a circuit breaker in the specified state.

    Args:
        state: The desired state for the circuit breaker.

    Returns:
        CircuitBreaker: A circuit breaker instance in the specified state.
    """
    cb = CircuitBreaker("test")
    match state:
        case CircuitBreakerState.OPEN:
            cb.transition_to_open()
        case CircuitBreakerState.HALF_OPEN:
            cb.transition_to_half_open()
        case CircuitBreakerState.CLOSED:
            cb.transition_to_closed()
        case CircuitBreakerState.FORCED_CLOSED:
            cb.transition_to_forced_closed()
        case CircuitBreakerState.FORCED_OPEN:
            cb.transition_to_forced_open()
    return cb


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
    cb.transition_to_open()
    return cb


@pytest.fixture
async def circuit_half_open() -> CircuitBreaker:
    """Fixture for a circuit breaker in the HALF_OPEN state."""
    cb = CircuitBreaker("half_open_circuit")
    cb.transition_to_half_open()
    return cb


@pytest.fixture
async def circuit_closed() -> CircuitBreaker:
    """Fixture for a circuit breaker in the CLOSED state."""
    cb = CircuitBreaker("closed_circuit")
    cb.transition_to_closed()
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
    error_info = ErrorDetails(
        time=datetime.now(tz=UTC),
        type=SentinelError.__name__,
        msg="This is a test error",
    )
    # Act
    error = CircuitBreakerError(
        name="test",
        last_error=error_info,
    )
    # Assert
    assert str(error) == "Circuit breaker 'test': call not permitted"
    assert error.last_error == error_info


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
    assert cb.state is CircuitBreakerState.HALF_OPEN


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
    state = cb.state
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
    assert cb.state is CircuitBreakerState.OPEN


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
    state = cb.state
    # Assert
    assert state is CircuitBreakerState.OPEN


@pytest.mark.anyio
async def test_circuit_half_open_raise_circuit_error(
    circuit_half_open: CircuitBreaker,
) -> None:
    """Test circuit breaker raises error when half-open and no success."""
    # Arrange
    circuit_half_open.half_open_capacity = 1
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
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize(
    ("ignore_errors", "error"),
    [
        (SentinelError, SentinelError),
        ((SentinelError, RuntimeError), SentinelError),
        ((ValueError, RuntimeError), RuntimeError),
    ],
)
async def test_circuit_ignore_errors(
    ignore_errors: type, error: type, state: CircuitBreakerState
) -> None:
    """Test circuit breaker transitions to closed state when ignoring errors."""
    # Arrange
    cb = create_circuit_breaker_in_state(state)
    cb.success_threshold = 1  # Avoid immediate closure

    # Act & Assert
    with pytest.raises(error):
        async with cb.guard(ignore_errors=ignore_errors):
            raise error()


@pytest.mark.anyio
async def test_circuit_breaker_last_error() -> None:
    """Test error info is properly recorded."""
    # Arrange
    cb = CircuitBreaker("test", error_threshold=1)
    # Act
    with suppress(SentinelError):
        async with cb.guard():
            raise sentinel_error
    # Assert
    assert cb.last_error == ErrorDetails(
        time=datetime.now(tz=UTC),
        type=sentinel_error.__class__.__name__,
        msg=str(sentinel_error),
    )


def test_circuit_metrics_initial() -> None:
    """Test metrics reflect circuit breaker state."""
    # Arrange
    cb = CircuitBreaker("test")

    # Act
    stats = cb.metrics()

    # Assert
    assert stats == CircuitBreakerMetrics(
        name="test",
        state=CircuitBreakerState.CLOSED,
        active_call_count=0,
        total_error_count=0,
        total_success_count=0,
        consecutive_error_count=0,
        consecutive_success_count=0,
        last_error=None,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize("success_count", [0, 1, 3, 5])
async def test_circuit_metrics_counters_with_successes(
    state: CircuitBreakerState, success_count: int
) -> None:
    """Test metrics in half-open state."""
    # Arrange
    cb = create_circuit_breaker_in_state(state)
    cb.success_threshold = (
        success_count + 1
    )  # Ensure it doesn't close immediately
    for _ in range(success_count):
        async with cb.guard():
            pass

    # Act
    stats = cb.metrics()

    # Assert
    assert stats.total_error_count == 0
    assert stats.total_success_count == success_count
    assert stats.consecutive_error_count == 0
    assert stats.consecutive_success_count == success_count


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state"),
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize("error_count", [0, 1, 3, 5])
async def test_circuit_metrics_counters_with_errors(
    state: CircuitBreakerState,
    error_count: int,
) -> None:
    """Test metrics with errors in various states."""
    # Arrange
    cb = create_circuit_breaker_in_state(state)
    cb.error_threshold = error_count + 1  # Ensure it doesn't open immediately
    for _ in range(error_count):
        with suppress(SentinelError):
            async with cb.guard():
                raise sentinel_error

    # Act
    stats = cb.metrics()

    # Assert
    assert stats.total_error_count == error_count
    assert stats.total_success_count == 0
    assert stats.consecutive_error_count == error_count
    assert stats.consecutive_success_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize("success_count", [0, 1, 3, 5])
async def test_circuit_metrics_counters_with_ignore_errors(
    state: CircuitBreakerState, success_count: int
) -> None:
    """Test metrics when errors are ignored."""
    # Arrange
    cb = create_circuit_breaker_in_state(state)
    cb.success_threshold = success_count + 1  # Avoid immediate closure
    for _ in range(success_count):
        with suppress(SentinelError):
            async with cb.guard(ignore_errors=SentinelError):
                raise sentinel_error

    # Act
    stats = cb.metrics()

    # Assert
    assert stats.total_error_count == 0
    assert stats.total_success_count == success_count
    assert stats.consecutive_error_count == 0
    assert stats.consecutive_success_count == success_count


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.OPEN,
        CircuitBreakerState.FORCED_CLOSED,
        CircuitBreakerState.FORCED_OPEN,
    ],
)
async def test_circuit_metrics_state(
    state: CircuitBreakerState,
) -> None:
    """Test metrics reflect state of the circuit breaker."""
    # Arrange
    cb = create_circuit_breaker_in_state(state)
    # Act
    stats = cb.metrics()
    # Assert
    assert stats.state == state


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize(
    "call_count",
    [0, 1, 3, 5],
)
async def test_circuit_metrics_active_call_count(
    state: CircuitBreakerState,
    call_count: int,
) -> None:
    """Test active call count in various states."""
    # Arrange
    cb = create_circuit_breaker_in_state(state)
    cb.success_threshold = call_count + 1  # Avoid immediate closure
    cb.half_open_capacity = call_count + 1
    assert cb.state == state
    # Act
    async with AsyncExitStack() as stack:
        for _ in range(call_count):
            await stack.enter_async_context(cb.guard())
        metrics = cb.metrics()

    # Assert
    assert metrics.active_call_count == call_count


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.OPEN,
        CircuitBreakerState.FORCED_OPEN,
        CircuitBreakerState.HALF_OPEN,
    ],
)
async def test_circuit_metrics_with_call_not_permitted(
    state: CircuitBreakerState,
) -> None:
    """Test metrics in OPEN and FORCED_OPEN states."""
    # Arrange
    cb = create_circuit_breaker_in_state(state)
    if state == CircuitBreakerState.HALF_OPEN:
        cb.half_open_capacity = 0  #  Ensure no calls are permitted
    with suppress(CircuitBreakerError):
        async with cb.guard():
            pass

    # Act
    metrics = cb.metrics()

    # Assert
    assert metrics == CircuitBreakerMetrics(
        name=cb.name,
        state=state,
        active_call_count=0,
        total_error_count=0,
        total_success_count=0,
        consecutive_error_count=0,
        consecutive_success_count=0,
        last_error=None,
    )
