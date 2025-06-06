"""Test CircuitBreaker implementation."""

import sys
from collections.abc import Iterator
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Union

import pytest
from anyio import to_thread
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
    CircuitBreakerState,
    ErrorDetails,
)


class SentinelError(Exception):
    """A sentinel error for testing purposes."""


sentinel_error = SentinelError("Sentinel error for testing")

FrozenTimeType = Union[
    "StepTickTimeFactory", "TickingDateTimeFactory", "FrozenDateTimeFactory"
]

ALL_STATES = [
    CircuitBreakerState.CLOSED,
    CircuitBreakerState.HALF_OPEN,
    CircuitBreakerState.OPEN,
    CircuitBreakerState.FORCED_CLOSED,
    CircuitBreakerState.FORCED_OPEN,
]

pytestmark = pytest.mark.anyio


async def transition(cb: CircuitBreaker, state: CircuitBreakerState) -> None:
    """Transition the circuit breaker to the specified state."""
    match state:
        case CircuitBreakerState.OPEN:
            await cb.transition_to_open()
        case CircuitBreakerState.HALF_OPEN:
            await cb.transition_to_half_open()
        case CircuitBreakerState.CLOSED:
            await cb.transition_to_closed()
        case CircuitBreakerState.FORCED_CLOSED:
            await cb.transition_to_forced_closed()
        case CircuitBreakerState.FORCED_OPEN:
            await cb.transition_to_forced_open()


async def create_circuit(
    state: CircuitBreakerState,
    ignore_exceptions: type[Exception] | tuple[type[Exception], ...] = (),
) -> CircuitBreaker:
    """Create a circuit breaker in the specified state."""
    cb = CircuitBreaker("test", ignore_exceptions=ignore_exceptions)
    await transition(cb, state)
    return cb


async def generate_success(cb: CircuitBreaker) -> None:
    """Generate a successful call in the circuit breaker."""
    async with cb:
        pass


async def generate_error(cb: CircuitBreaker) -> None:
    """Generate an error call in the circuit breaker."""
    with suppress(SentinelError):
        async with cb:
            raise sentinel_error


@pytest.fixture(autouse=True)
def frozen_time() -> Iterator[FrozenTimeType]:
    """Freeze time for the duration of the test."""
    with freeze_time() as frozen:
        yield frozen


@pytest.fixture(
    params=[
        CircuitBreakerState.OPEN,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_OPEN,
    ]
)
async def circuit_call_not_permitted(
    request: pytest.FixtureRequest,
) -> CircuitBreaker:
    """Fixture for circuit breakers that do not permit calls."""
    cb = await create_circuit(request.param)
    cb.half_open_capacity = 0  # Ensure no calls are permitted
    return cb


@pytest.fixture(
    params=[
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ]
)
async def circuit_call_permitted(
    request: pytest.FixtureRequest,
) -> CircuitBreaker:
    """Fixture for circuit breakers that permit calls."""
    cb = await create_circuit(request.param)
    cb.error_threshold = sys.maxsize
    cb.success_threshold = sys.maxsize
    return cb


def test_circuit_init() -> None:
    """Test circuit breaker initialization."""
    # Act
    cb = CircuitBreaker("test")

    # Assert
    assert cb.name == "test"


def test_circuit_from_thread_init() -> None:
    """Test from_thread initialization."""
    # Arrange
    cb = CircuitBreaker("test")

    # Act & Assert
    assert cb.from_thread


def test_circuit_initial_state() -> None:
    """Test circuit breaker initial state."""
    # Arrange
    cb = CircuitBreaker("test")

    # Assert
    assert cb.state is CircuitBreakerState.CLOSED


async def test_circuit_protect_success() -> None:
    """Test from_thread protect."""
    # Arrange
    cb = CircuitBreaker("test")

    # Act & Assert
    await generate_success(cb)


async def test_circuit_from_thread_protect_success() -> None:
    """Test from_thread.protect allows successful sync call."""
    cb = CircuitBreaker("test")

    def sync() -> None:
        with cb.from_thread:
            pass

    # Act
    await to_thread.run_sync(sync)


async def test_circuit_decorator_with_call_permitted(
    circuit_call_permitted: CircuitBreaker,
) -> None:
    """Test circuit breaker decorator with success."""

    # Arrange
    @circuit_call_permitted
    async def protected_function() -> None:
        pass

    @circuit_call_permitted()
    async def another_protected_function(
        pos: str, kwarg: str = "default"
    ) -> bool:
        return bool(pos == "positional" and kwarg == "keyword")

    # Act & Assert
    await protected_function()
    assert await another_protected_function("positional", kwarg="keyword")


async def test_circuit_from_thread_decorator_with_call_permitted(
    circuit_call_permitted: CircuitBreaker,
) -> None:
    """Test from_thread decorator with success."""

    # Arrange
    @circuit_call_permitted
    def protected_function() -> None:
        pass

    @circuit_call_permitted()
    def another_protected_function(pos: str) -> bool:
        return bool(pos == "positional")

    # Act & Assert
    await to_thread.run_sync(protected_function)
    await to_thread.run_sync(another_protected_function, "positional")


async def test_circuit_error_raises(
    circuit_call_permitted: CircuitBreaker,
) -> None:
    """Test from_thread protect."""
    # Act & Assert
    with pytest.raises(SentinelError):
        async with circuit_call_permitted:
            raise sentinel_error


async def test_circuitbreaker_from_thread_error_raises(
    circuit_call_permitted: CircuitBreaker,
) -> None:
    """from_thread.protect raises on error."""

    # Arrange
    def sync() -> None:
        with circuit_call_permitted.from_thread:
            raise sentinel_error

    # Act & Assert
    with pytest.raises(SentinelError):
        await to_thread.run_sync(sync)


async def test_circuit_decorator_error_raises(
    circuit_call_permitted: CircuitBreaker,
) -> None:
    """Test circuit breaker decorator with error."""

    # Arrange
    @circuit_call_permitted
    async def protected_function() -> None:
        raise sentinel_error

    @circuit_call_permitted()
    async def another_protected_function() -> None:
        raise sentinel_error

    # Act & Assert
    with pytest.raises(SentinelError):
        await protected_function()
    with pytest.raises(SentinelError):
        await another_protected_function()


async def test_circuit_from_thread_decorator_error_raises(
    circuit_call_permitted: CircuitBreaker,
) -> None:
    """Test from_thread decorator with error."""

    @circuit_call_permitted
    def protected_function() -> None:
        raise sentinel_error

    @circuit_call_permitted()
    def another_protected_function() -> None:
        raise sentinel_error

    # Act & Assert
    with pytest.raises(SentinelError):
        await to_thread.run_sync(protected_function)
    with pytest.raises(SentinelError):
        await to_thread.run_sync(another_protected_function)


async def test_circuit_with_call_not_permitted(
    circuit_call_not_permitted: CircuitBreaker,
) -> None:
    """Test circuit breaker raises CircuitBreakerError when open."""
    # Act & Assert
    with pytest.raises(CircuitBreakerError):
        async with circuit_call_not_permitted:
            pytest.fail("Expected not reached")


async def test_circuit_from_thread_with_call_not_permitted(
    circuit_call_not_permitted: CircuitBreaker,
) -> None:
    """Test from_thread protect raises CircuitBreakerError when not permitted."""

    # Arrange
    def sync() -> None:
        with circuit_call_not_permitted.from_thread:
            pytest.fail("Expected not reached")

    # Act & Assert
    with pytest.raises(CircuitBreakerError):
        await to_thread.run_sync(sync)


async def test_circuit_decorator_with_call_not_permitted(
    circuit_call_not_permitted: CircuitBreaker,
) -> None:
    """Test circuit breaker decorator raises CircuitBreakerError when open."""

    # Arrange
    @circuit_call_not_permitted
    async def protected_function() -> None:
        pytest.fail("Expected not reached")

    @circuit_call_not_permitted()
    async def another_protected_function() -> None:
        pytest.fail("Expected not reached")

    # Act & Assert
    with pytest.raises(CircuitBreakerError):
        await protected_function()
    with pytest.raises(CircuitBreakerError):
        await another_protected_function()


async def test_circuit_from_thread_decorator_with_call_not_permitted(
    circuit_call_not_permitted: CircuitBreaker,
) -> None:
    """Test from_thread decorator raises CircuitBreakerError when not permitted."""

    # Arrange
    @circuit_call_not_permitted
    def protected_function() -> None:
        pytest.fail("Expected not reached")

    @circuit_call_not_permitted()
    def another_protected_function() -> None:
        pytest.fail("Expected not reached")

    # Act & Assert
    with pytest.raises(CircuitBreakerError):
        await to_thread.run_sync(protected_function)
    with pytest.raises(CircuitBreakerError):
        await to_thread.run_sync(another_protected_function)


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


async def test_circuit_transition_to_half_open_after_timeout(
    frozen_time: FrozenTimeType,
) -> None:
    """Test circuit breaker transitions to half-open after reset timeout."""
    # Arrange
    cb = await create_circuit(CircuitBreakerState.OPEN)
    cb.success_threshold = 2  # Ensure it doesn't close immediately
    frozen_time.tick(timedelta(seconds=cb.reset_timeout))

    # Act
    await generate_success(cb)

    # Assert
    assert cb.state is CircuitBreakerState.HALF_OPEN


@pytest.mark.parametrize("reset_timeout", [0.5, 1, 30])
async def test_circuit_not_transition_to_half_open_before_timeout(
    frozen_time: FrozenTimeType, reset_timeout: float
) -> None:
    """Test circuit breaker does not transition to half-open before reset timeout."""
    # Arrange
    cb = await create_circuit(CircuitBreakerState.OPEN)
    cb.reset_timeout = reset_timeout
    frozen_time.tick(
        timedelta(seconds=cb.reset_timeout) - timedelta(milliseconds=1)
    )  # Ensure not enough time has passed

    # Act & Assert
    with pytest.raises(CircuitBreakerError):
        await generate_success(cb)
    assert cb.state == CircuitBreakerState.OPEN


@pytest.mark.parametrize("success_count", [1, 3, 5])
async def test_circuit_transition_to_closed(success_count: int) -> None:
    """Test circuit breaker closes after success threshold in half-open."""
    # Arrange
    cb = await create_circuit(CircuitBreakerState.HALF_OPEN)
    cb.success_threshold = success_count

    # Act & Assert
    for _ in range(success_count):
        assert cb.state == CircuitBreakerState.HALF_OPEN
        await generate_success(cb)
    assert cb.state == CircuitBreakerState.CLOSED


@pytest.mark.parametrize("error_count", [1, 3, 5])
async def test_circuit_transition_from_half_open_to_open(
    error_count: int,
) -> None:
    """Test circuit breaker transitions to open after errors in half-open."""
    # Arrange
    cb = await create_circuit(CircuitBreakerState.HALF_OPEN)
    cb.error_threshold = error_count

    # Act & Assert
    for _ in range(error_count):
        assert cb.state == CircuitBreakerState.HALF_OPEN
        await generate_error(cb)
    assert cb.state == CircuitBreakerState.OPEN


@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize(
    ("ignore_exceptions", "error"),
    [
        (SentinelError, SentinelError),
        ((SentinelError, RuntimeError), SentinelError),
        ((ValueError, RuntimeError), RuntimeError),
    ],
)
async def test_circuit_with_ignore_exceptions(
    ignore_exceptions: type, error: type, state: CircuitBreakerState
) -> None:
    """Test circuit breaker transitions to closed state when ignoring errors."""
    # Arrange
    cb = await create_circuit(state, ignore_exceptions=ignore_exceptions)
    cb.success_threshold = 1  # Avoid immediate closure

    # Act & Assert
    with pytest.raises(error):
        async with cb:
            raise error()


@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize(
    ("ignore_exceptions", "error"),
    [
        (SentinelError, SentinelError),
        ((SentinelError, RuntimeError), SentinelError),
        ((ValueError, RuntimeError), RuntimeError),
    ],
)
async def test_circuit_from_thread_with_ignore_exceptions(
    ignore_exceptions: type, error: type, state: CircuitBreakerState
) -> None:
    """Test from_thread protect ignores specified error in various states."""
    # Arrange
    cb = await create_circuit(state, ignore_exceptions=ignore_exceptions)
    cb.success_threshold = 1  # Avoid immediate closure

    def sync() -> None:
        with cb.from_thread:
            raise error()

    # Act & Assert
    with pytest.raises(error):
        await to_thread.run_sync(sync)


async def test_circuit_breaker_last_error() -> None:
    """Test error info is properly recorded."""
    # Arrange
    cb = CircuitBreaker("test")

    # Act
    with suppress(SentinelError):
        async with cb:
            raise sentinel_error

    # Assert
    assert cb.last_error == sentinel_error
    assert cb.last_error_time == datetime.now(UTC)


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
        active_calls=0,
        total_error_count=0,
        total_success_count=0,
        consecutive_error_count=0,
        consecutive_success_count=0,
        last_error=None,
    )


@pytest.mark.parametrize("success_count", [0, 1, 3, 5])
async def test_circuit_metrics_counters_with_successes(
    circuit_call_permitted: CircuitBreaker, success_count: int
) -> None:
    """Test metrics in half-open state."""
    # Arrange
    for _ in range(success_count):
        async with circuit_call_permitted:
            pass

    # Act
    stats = circuit_call_permitted.metrics()

    # Assert
    assert stats == CircuitBreakerMetrics(
        name=circuit_call_permitted.name,
        state=circuit_call_permitted.state,
        active_calls=0,
        total_error_count=0,
        total_success_count=success_count,
        consecutive_error_count=0,
        consecutive_success_count=success_count,
        last_error=None,
    )


@pytest.mark.parametrize("error_count", [1, 3, 5])
async def test_circuit_metrics_with_errors(
    circuit_call_permitted: CircuitBreaker,
    error_count: int,
) -> None:
    """Test metrics with errors in various states."""
    # Arrange
    for _ in range(error_count):
        await generate_error(circuit_call_permitted)

    # Act
    stats = circuit_call_permitted.metrics()

    # Assert
    assert stats == CircuitBreakerMetrics(
        name=circuit_call_permitted.name,
        state=circuit_call_permitted.state,
        active_calls=0,
        total_error_count=error_count,
        total_success_count=0,
        consecutive_error_count=error_count,
        consecutive_success_count=0,
        last_error=ErrorDetails(
            type=SentinelError.__name__,
            msg=str(sentinel_error),
            time=datetime.now(UTC),
        ),
    )


@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
@pytest.mark.parametrize("success_count", [0, 1, 3, 5])
async def test_circuit_metrics_counters_with_ignore_exceptions(
    state: CircuitBreakerState, success_count: int
) -> None:
    """Test metrics when errors are ignored."""
    # Arrange
    cb = await create_circuit(state, ignore_exceptions=SentinelError)
    cb.success_threshold = success_count + 1  # Avoid immediate closure
    for _ in range(success_count):
        with suppress(SentinelError):
            async with cb:
                raise sentinel_error

    # Act
    stats = cb.metrics()

    # Assert
    assert stats == CircuitBreakerMetrics(
        name=cb.name,
        state=state,
        active_calls=0,
        total_error_count=0,
        total_success_count=success_count,
        consecutive_error_count=0,
        consecutive_success_count=success_count,
        last_error=None,
    )


@pytest.mark.parametrize(
    "call_count",
    [0, 1, 3, 5],
)
async def test_circuit_metrics_active_calls(
    circuit_call_permitted: CircuitBreaker, call_count: int
) -> None:
    """Active call count is correct for each state."""
    # Arrange
    circuit_call_permitted.half_open_capacity = call_count + 1

    # Act
    async with AsyncExitStack() as stack:
        for _ in range(call_count):
            await stack.enter_async_context(circuit_call_permitted)
        metrics = circuit_call_permitted.metrics()

    # Assert
    assert metrics.active_calls == call_count


async def test_circuit_metrics_with_call_not_permitted(
    circuit_call_not_permitted: CircuitBreaker,
) -> None:
    """Test metrics in OPEN and FORCED_OPEN states."""
    # Arrange
    with suppress(CircuitBreakerError):
        await generate_success(circuit_call_not_permitted)

    # Act
    metrics = circuit_call_not_permitted.metrics()

    # Assert
    assert metrics == CircuitBreakerMetrics(
        name=circuit_call_not_permitted.name,
        state=circuit_call_not_permitted.state,
        active_calls=0,
        total_error_count=0,
        total_success_count=0,
        consecutive_error_count=0,
        consecutive_success_count=0,
        last_error=None,
    )


async def test_circuit_restart() -> None:
    """Test circuit breaker restarts after forced open."""
    # Arrange
    cb = CircuitBreaker("test")
    await generate_error(cb)
    await generate_success(cb)

    # Act
    await cb.restart()

    # Assert
    assert cb.metrics() == CircuitBreakerMetrics(
        name="test",
        state=CircuitBreakerState.CLOSED,
        active_calls=0,
        total_error_count=0,
        total_success_count=0,
        consecutive_error_count=0,
        consecutive_success_count=0,
        last_error=None,
    )


async def test_circuit_from_thread_restart() -> None:
    """Test circuit breaker restarts after forced open using from_thread."""
    # Arrange
    cb = CircuitBreaker("test")
    await generate_error(cb)
    await generate_success(cb)

    # Act
    def sync() -> None:
        cb.from_thread.restart()

    await to_thread.run_sync(sync)

    # Assert
    assert cb.metrics() == CircuitBreakerMetrics(
        name="test",
        state=CircuitBreakerState.CLOSED,
        active_calls=0,
        total_error_count=0,
        total_success_count=0,
        consecutive_error_count=0,
        consecutive_success_count=0,
        last_error=None,
    )


@pytest.mark.parametrize("from_state", ALL_STATES)
@pytest.mark.parametrize("to_state", ALL_STATES)
async def test_circuit_state_transition(
    from_state: CircuitBreakerState,
    to_state: CircuitBreakerState,
) -> None:
    """Test explicit state transition methods."""
    # Arrange
    cb = await create_circuit(from_state)

    # Act
    await transition(cb, to_state)

    # Assert
    assert cb.state == to_state


@pytest.mark.parametrize("from_state", ALL_STATES)
@pytest.mark.parametrize("to_state", ALL_STATES)
async def test_circuit_from_thread_state_transition(
    from_state: CircuitBreakerState,
    to_state: CircuitBreakerState,
) -> None:
    """Test explicit state transition methods using from_thread."""
    cb = await create_circuit(from_state)

    def sync_transition() -> None:
        match to_state:
            case CircuitBreakerState.OPEN:
                cb.from_thread.transition_to_open()
            case CircuitBreakerState.HALF_OPEN:
                cb.from_thread.transition_to_half_open()
            case CircuitBreakerState.CLOSED:
                cb.from_thread.transition_to_closed()
            case CircuitBreakerState.FORCED_CLOSED:
                cb.from_thread.transition_to_forced_closed()
            case CircuitBreakerState.FORCED_OPEN:
                cb.from_thread.transition_to_forced_open()

    await to_thread.run_sync(sync_transition)
    assert cb.state == to_state


@pytest.mark.parametrize(
    "level", ["WARNING", "DEBUG", "INFO", "ERROR", "CRITICAL"]
)
def test_circuitbreaker_log_level(
    level: Literal["WARNING", "DEBUG", "INFO", "ERROR", "CRITICAL"],
) -> None:
    """Test log_level property getter and setter."""
    # Arrange
    cb = CircuitBreaker("test")

    # Act
    cb.log_level = level

    # Assert
    assert cb.log_level == level
