"""Test CircuitBreaker implementation."""

import logging
import sys
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime
from typing import Literal

import pydantic
import pytest
from anyio import to_thread
from freezegun import freeze_time

from grelmicro.resilience import circuitbreaker
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
    *,
    ignore_exceptions: type[Exception] | tuple[type[Exception], ...] = (),
    error_threshold: int | None = None,
    success_threshold: int | None = None,
    reset_timeout: float | None = None,
    half_open_capacity: int | None = None,
) -> CircuitBreaker:
    """Create a circuit breaker in the specified state."""
    cb = CircuitBreaker(
        "test",
        ignore_exceptions=ignore_exceptions,
        error_threshold=error_threshold,
        success_threshold=success_threshold,
        reset_timeout=reset_timeout,
        half_open_capacity=half_open_capacity,
    )
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
    # `half_open_capacity=1` is the minimum. For HALF_OPEN, we saturate
    # the slot below so any further call is rejected.
    cb = await create_circuit(request.param, half_open_capacity=1)
    if request.param == CircuitBreakerState.HALF_OPEN:
        await cb._try_acquire_call(cb.config)
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
    return await create_circuit(
        request.param,
        error_threshold=sys.maxsize,
        success_threshold=sys.maxsize,
    )


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test circuit breaker transitions to half-open after reset timeout."""
    # Arrange
    cb = await create_circuit(
        CircuitBreakerState.OPEN, success_threshold=2
    )  # Ensure it doesn't close immediately
    monkeypatch.setattr(
        circuitbreaker, "monotonic", lambda: cb._open_until_time
    )

    # Act
    await generate_success(cb)

    # Assert
    assert cb.state is CircuitBreakerState.HALF_OPEN


@pytest.mark.parametrize("reset_timeout", [0.5, 1, 30])
async def test_circuit_not_transition_to_half_open_before_timeout(
    monkeypatch: pytest.MonkeyPatch, reset_timeout: float
) -> None:
    """Test circuit breaker does not transition to half-open before reset timeout."""
    # Arrange
    cb = await create_circuit(
        CircuitBreakerState.OPEN, reset_timeout=reset_timeout
    )
    monkeypatch.setattr(
        circuitbreaker, "monotonic", lambda: cb._open_until_time - 0.001
    )

    # Act & Assert
    with pytest.raises(CircuitBreakerError):
        await generate_success(cb)
    assert cb.state == CircuitBreakerState.OPEN


@pytest.mark.parametrize("success_count", [1, 3, 5])
async def test_circuit_transition_to_closed(success_count: int) -> None:
    """Test circuit breaker closes after success threshold in half-open."""
    # Arrange
    cb = await create_circuit(
        CircuitBreakerState.HALF_OPEN,
        success_threshold=success_count,
        half_open_capacity=success_count,
    )

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
    cb = await create_circuit(
        CircuitBreakerState.HALF_OPEN,
        error_threshold=error_count,
        half_open_capacity=error_count,
    )

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
    ignore_exceptions: type[Exception] | tuple[type[Exception], ...],
    error: type[Exception],
    state: CircuitBreakerState,
) -> None:
    """Test circuit breaker transitions to closed state when ignoring errors."""
    # Arrange
    cb = await create_circuit(
        state,
        ignore_exceptions=ignore_exceptions,
        success_threshold=1,
    )  # success_threshold=1 avoids immediate closure

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
    ignore_exceptions: type[Exception] | tuple[type[Exception], ...],
    error: type[Exception],
    state: CircuitBreakerState,
) -> None:
    """Test from_thread protect ignores specified error in various states."""
    # Arrange
    cb = await create_circuit(
        state,
        ignore_exceptions=ignore_exceptions,
        success_threshold=1,
    )  # success_threshold=1 avoids immediate closure

    def sync() -> None:
        with cb.from_thread:
            raise error()

    # Act & Assert
    with pytest.raises(error):
        await to_thread.run_sync(sync)


@freeze_time()
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
@freeze_time()
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
    cb = await create_circuit(
        state,
        ignore_exceptions=SentinelError,
        success_threshold=success_count + 1,
    )  # success_threshold=count+1 avoids immediate closure
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
@pytest.mark.parametrize(
    "state",
    [
        CircuitBreakerState.CLOSED,
        CircuitBreakerState.HALF_OPEN,
        CircuitBreakerState.FORCED_CLOSED,
    ],
)
async def test_circuit_metrics_active_calls(
    state: CircuitBreakerState, call_count: int
) -> None:
    """Active call count is correct for each state."""
    # Arrange
    cb = await create_circuit(
        state,
        error_threshold=sys.maxsize,
        success_threshold=sys.maxsize,
        half_open_capacity=call_count + 1,
    )

    # Act
    async with AsyncExitStack() as stack:
        for _ in range(call_count):
            await stack.enter_async_context(cb)
        metrics = cb.metrics()

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
    # HALF_OPEN's slot is saturated by the fixture to make the call
    # not permitted, so active_calls reflects the saturating call.
    expected_active = (
        1
        if circuit_call_not_permitted.state == CircuitBreakerState.HALF_OPEN
        else 0
    )
    assert metrics == CircuitBreakerMetrics(
        name=circuit_call_not_permitted.name,
        state=circuit_call_not_permitted.state,
        active_calls=expected_active,
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
    """`log_level` is read from the frozen config."""
    # Act
    cb = CircuitBreaker("test", log_level=level)

    # Assert
    assert cb.config.log_level == level


# --- reconfigure ---


async def test_reconfigure_swaps_config() -> None:
    """Reconfigure publishes the new config."""
    cb = CircuitBreaker("rc", error_threshold=5)
    new_config = cb.config.model_copy(update={"error_threshold": 10})

    await cb.reconfigure(new_config)

    assert cb.config == new_config


async def test_reconfigure_same_config_is_noop() -> None:
    """Equal configs short-circuit."""
    cb = CircuitBreaker("rc", error_threshold=5)
    same = cb.config.model_copy()

    await cb.reconfigure(same)

    assert cb.config == same


async def test_reconfigure_preserves_runtime_state() -> None:
    """A swap does not reset state, counters, or last_error."""
    cb = CircuitBreaker("rc", error_threshold=2)
    boom = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        async with cb:
            raise boom

    error_count_before = cb.metrics().total_error_count
    last_error_before = cb.last_error

    new_config = cb.config.model_copy(update={"error_threshold": 10})
    await cb.reconfigure(new_config)

    assert cb.metrics().total_error_count == error_count_before
    assert cb.last_error is last_error_before


async def test_reconfigure_updates_logger_level() -> None:
    """`log_level` change propagates to the instance logger."""
    cb = CircuitBreaker("rc", log_level="WARNING")
    assert cb._logger.level == logging.WARNING

    await cb.reconfigure(cb.config.model_copy(update={"log_level": "DEBUG"}))

    assert cb._logger.level == logging.DEBUG


async def test_reconfigure_changes_error_threshold_for_next_call() -> None:
    """Error threshold change applies to the next call without resetting counters."""
    cb = CircuitBreaker("rc", error_threshold=5)
    new_config = cb.config.model_copy(update={"error_threshold": 1})

    await cb.reconfigure(new_config)

    boom = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        async with cb:
            raise boom

    assert cb.state == CircuitBreakerState.OPEN


async def test_reconfigure_rejects_different_config_type() -> None:
    """The mixin rejects config types different from the current one."""

    class Other(pydantic.BaseModel):
        pass

    cb = CircuitBreaker("rc")
    with pytest.raises(TypeError, match="CircuitBreakerConfig"):
        await cb.reconfigure(Other())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
