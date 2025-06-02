"""Test CircuitBreaker implementation."""

from collections.abc import Iterator
from contextlib import AsyncExitStack, suppress
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
    """Create a circuit breaker in the specified state."""
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


async def generate_success(cb: CircuitBreaker) -> None:
    """Generate a successful call in the circuit breaker."""
    async with cb.guard():
        pass


async def generate_error(cb: CircuitBreaker) -> None:
    """Generate an error call in the circuit breaker."""
    with suppress(SentinelError):
        async with cb.guard():
            raise sentinel_error


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
    with freeze_time() as frozen:
        yield frozen


def test_circuitbreaker_creation() -> None:
    """Test creating a circuit breaker."""
    # Act
    cb = CircuitBreaker("test")

    # Assert
    assert cb.name == "test"


def test_circuitbreaker_singleton() -> None:
    """Test circuit breakers are singletons by name."""
    # Arrange
    cb1 = CircuitBreaker("test")

    # Act
    cb2 = CircuitBreaker("test")

    # Assert
    assert cb1 is cb2


def test_registry_get_all() -> None:
    """Test CircuitBreakerRegistry.get_all returns all circuit breakers."""
    # Arrange
    cb1 = CircuitBreaker("cb1")
    cb2 = CircuitBreaker("cb2")

    # Act
    all_cb = CircuitBreakerRegistry.get_all()

    # Assert
    assert all_cb == [cb1, cb2]


def test_registry_get() -> None:
    """Test CircuitBreakerRegistry.get returns correct circuit breaker or None."""
    # Arrange
    cb1 = CircuitBreaker("cb1")

    # Act
    get_cb1 = CircuitBreakerRegistry.get("cb1")
    get_none = CircuitBreakerRegistry.get("non-existent")

    # Assert
    assert get_cb1 is cb1
    assert get_none is None


def test_circuit_initial_state() -> None:
    """Test circuit breaker initial state."""
    # Arrange
    cb = CircuitBreaker("test")

    # Assert
    assert cb.state is CircuitBreakerState.CLOSED


@pytest.mark.anyio
@pytest.mark.parametrize("error_count", [1, 3, 5])
async def test_circuit_transition_to_open(error_count: int) -> None:
    """Test circuit breaker opens after threshold errors."""
    # Arrange
    cb = CircuitBreaker("test", error_threshold=error_count)

    # Act
    for _ in range(error_count):
        await generate_error(cb)

    # Assert
    assert cb.state == CircuitBreakerState.OPEN


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.OPEN,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_OPEN,
    ],
)
async def test_circuit_with_call_not_permitted(
    state: CircuitBreakerState,
) -> None:
    """Test circuit breaker raises CircuitBreakerError when open."""
    # Arrange
    cb = create_circuit_breaker_in_state(state)
    if state == CircuitBreakerState.HALF_OPEN:
        cb.half_open_capacity = 0  # Ensure no calls are permitted

    # Act & Assert
    with pytest.raises(CircuitBreakerError):
        async with cb.guard():
            pytest.fail("Expected not reached")


def test_circuit_breaker_error() -> None:
    """Test CircuitBreakerError."""
    # Arrange
    error_details = ErrorDetails(
        time=datetime.now(tz=UTC),
        type=SentinelError.__name__,
        msg="This is a test error",
    )

    # Act
    error = CircuitBreakerError(
        name="test",
        last_error=error_details,
    )

    # Assert
    assert str(error) == "Circuit breaker 'test': call not permitted"
    assert error.last_error == error_details


@pytest.mark.anyio
@pytest.mark.parametrize("trigger", ["call", "get_state"])
async def test_circuit_transition_to_half_open_after_timeout(
    frozen_time: FrozenTimeType,
    trigger: str,
) -> None:
    """Test circuit breaker transitions to half-open after reset timeout."""
    # Arrange
    cb = create_circuit_breaker_in_state(CircuitBreakerState.OPEN)
    cb.success_threshold = 2  # Ensure it doesn't close immediately
    frozen_time.tick(timedelta(seconds=cb.reset_timeout))

    # Act
    if trigger == "call":
        await generate_success(cb)
        state = cb._state
    else:
        state = cb.state

    # Assert
    assert state is CircuitBreakerState.HALF_OPEN


@pytest.mark.anyio
@pytest.mark.parametrize("trigger", ["call", "get_state"])
@pytest.mark.parametrize("reset_timeout", [0.5, 1, 30])
async def test_circuit_not_transition_to_half_open_before_timeout(
    frozen_time: FrozenTimeType, reset_timeout: float, trigger: str
) -> None:
    """Test circuit breaker does not transition to half-open before reset timeout."""
    # Arrange
    cb = create_circuit_breaker_in_state(CircuitBreakerState.OPEN)
    cb.reset_timeout = reset_timeout
    frozen_time.tick(
        timedelta(seconds=cb.reset_timeout) - timedelta(milliseconds=1)
    )  # Ensure not enough time has passed

    # Act & Assert
    if trigger == "call":
        with pytest.raises(CircuitBreakerError):
            await generate_success(cb)
    else:
        state = cb.state

    # Assert
    assert state == CircuitBreakerState.OPEN


@pytest.mark.anyio
@pytest.mark.parametrize("success_count", [1, 3, 5])
async def test_circuit_transition_to_closed(success_count: int) -> None:
    """Test circuit breaker closes after success threshold in half-open."""
    # Arrange
    cb = create_circuit_breaker_in_state(CircuitBreakerState.HALF_OPEN)
    cb.success_threshold = success_count

    # Act & Assert
    for _ in range(success_count):
        assert cb.state == CircuitBreakerState.HALF_OPEN
        await generate_success(cb)
    assert cb.state == CircuitBreakerState.CLOSED


@pytest.mark.anyio
@pytest.mark.parametrize("error_count", [1, 3, 5])
async def test_circuit_transition_from_half_open_to_open(
    error_count: int,
) -> None:
    """Test circuit breaker transitions to open after failures in half-open."""
    # Arrange
    cb = create_circuit_breaker_in_state(CircuitBreakerState.HALF_OPEN)
    cb.error_threshold = error_count

    # Act & Assert
    for _ in range(error_count):
        assert cb.state == CircuitBreakerState.HALF_OPEN
        await generate_error(cb)
    assert cb.state == CircuitBreakerState.OPEN


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
        await generate_success(cb)

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


@pytest.mark.anyio
async def test_circuit_restart() -> None:
    """Test circuit breaker restarts after forced open."""
    # Arrange
    cb = CircuitBreaker("test")
    await generate_error(cb)
    await generate_success(cb)

    # Act
    cb.restart()

    # Assert
    assert cb.metrics() == CircuitBreakerMetrics(
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
    "from_state",
    [
        CircuitBreakerState.OPEN,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_OPEN,
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize(
    ("to_state", "transition_func"),
    [
        (CircuitBreakerState.CLOSED, "transition_to_closed"),
        (CircuitBreakerState.OPEN, "transition_to_open"),
        (CircuitBreakerState.HALF_OPEN, "transition_to_half_open"),
        (CircuitBreakerState.FORCED_OPEN, "transition_to_forced_open"),
        (CircuitBreakerState.FORCED_CLOSED, "transition_to_forced_closed"),
    ],
)
async def test_state_transition_methods(
    from_state: CircuitBreakerState,
    to_state: CircuitBreakerState,
    transition_func: str,
) -> None:
    """Test explicit state transition methods.

    This test verifies that each state transition method correctly changes
    the circuit breaker's state, regardless of its initial state. It ensures
    that transitions between any two states are possible using the appropriate
    transition method.

    Args:
        from_state: The initial state of the circuit breaker.
        to_state: The target state after transition.
        transition_func: The name of the transition method to call.
    """
    # Arrange
    cb = create_circuit_breaker_in_state(from_state)

    # Act
    getattr(cb, transition_func)()

    # Assert
    assert cb.state == to_state
