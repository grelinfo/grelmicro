"""Memory circuit-breaker adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, Self

from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    CircuitBreakerSnapshot,
    CircuitBreakerStrategy,
)
from grelmicro.resilience.circuitbreaker import CircuitBreakerState

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.resilience.circuitbreaker.consecutive_count import (
        ConsecutiveCountConfig,
    )


@dataclass(slots=True)
class _BreakerState:
    """Per-breaker mutable state held by `MemoryCircuitBreakerAdapter`."""

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    opened_at: float = 0.0
    cool_down: float = 0.0
    consecutive_error_count: int = 0
    consecutive_success_count: int = 0
    half_open_admit: int = 0


class MemoryCircuitBreakerAdapter(CircuitBreakerBackend):
    """In-memory circuit breaker adapter.

    State for every breaker bound to this adapter is held in process,
    keyed by breaker name. Closing the adapter clears every breaker's
    state so the next start begins on a clean slate.

    Use it for tests and single-process deployments. Use
    `RedisCircuitBreakerAdapter` for fleet-wide shared state.
    """

    is_shared: ClassVar[bool] = False

    def __init__(self) -> None:
        """Initialize the circuit breaker adapter."""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._states: dict[str, _BreakerState] = {}

    async def __aenter__(self) -> Self:
        """Open the adapter and capture the running loop."""
        self._loop = asyncio.get_running_loop()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the adapter and clear every breaker's state."""
        self._states.clear()
        self._loop = None

    def bind(
        self,
        *,
        name: str,
        config: ConsecutiveCountConfig,
    ) -> CircuitBreakerStrategy:
        """Build a strategy bound to this adapter's per-name state.

        Two breakers constructed with the same ``name`` against the same
        adapter share the same `_BreakerState` entry, mirroring the
        Redis adapter's per-name keying.
        """
        return _MemoryConsecutiveCountStrategy(
            states=self._states,
            name=name,
            config=config,
        )


class _MemoryConsecutiveCountStrategy(CircuitBreakerStrategy):
    """In-memory consecutive-count strategy.

    Mirrors the Redis Lua semantics with `monotonic` time. Each method
    runs synchronously on the loop thread so no lock is needed.
    """

    def __init__(
        self,
        *,
        states: dict[str, _BreakerState],
        name: str,
        config: ConsecutiveCountConfig,
    ) -> None:
        """Bind the strategy to the breaker's per-name state and config."""
        self._states = states
        self._name = name
        self._error_threshold = config.error_threshold
        self._success_threshold = config.success_threshold
        self._reset_timeout = config.reset_timeout
        self._half_open_capacity = config.half_open_capacity

    def _get_or_create(self) -> _BreakerState:
        state = self._states.get(self._name)
        if state is None:
            state = _BreakerState()
            self._states[self._name] = state
        return state

    async def try_acquire(self) -> bool:
        """Atomic admission in the loop thread."""
        state = self._get_or_create()

        if state.state in (
            CircuitBreakerState.CLOSED,
            CircuitBreakerState.FORCED_CLOSED,
        ):
            return True

        if state.state == CircuitBreakerState.FORCED_OPEN:
            return False

        if state.state == CircuitBreakerState.OPEN:
            if monotonic() >= state.opened_at + state.cool_down:
                state.state = CircuitBreakerState.HALF_OPEN
                state.consecutive_error_count = 0
                state.consecutive_success_count = 0
                state.half_open_admit = 0
                state.opened_at = 0.0
                state.cool_down = 0.0
            else:
                return False

        if (
            state.state == CircuitBreakerState.HALF_OPEN
            and state.half_open_admit < self._half_open_capacity
        ):
            state.half_open_admit += 1
            return True

        return False

    async def record_outcome(
        self,
        *,
        success: bool,
        duration: float = 0.0,  # noqa: ARG002
    ) -> CircuitBreakerSnapshot:
        """Record a call outcome and apply any state transition."""
        state = self._get_or_create()

        if state.state in (
            CircuitBreakerState.FORCED_OPEN,
            CircuitBreakerState.FORCED_CLOSED,
            CircuitBreakerState.OPEN,
        ):
            return _snapshot_of(state)

        if success:
            state.consecutive_success_count += 1
            state.consecutive_error_count = 0
            if state.state == CircuitBreakerState.HALF_OPEN:
                if state.half_open_admit > 0:
                    state.half_open_admit -= 1
                if state.consecutive_success_count >= self._success_threshold:
                    state.state = CircuitBreakerState.CLOSED
                    state.consecutive_success_count = 0
                    state.consecutive_error_count = 0
                    state.half_open_admit = 0
                    state.opened_at = 0.0
                    state.cool_down = 0.0
        else:
            state.consecutive_error_count += 1
            state.consecutive_success_count = 0
            if (
                state.state == CircuitBreakerState.HALF_OPEN
                and state.half_open_admit > 0
            ):
                state.half_open_admit -= 1
            if state.consecutive_error_count >= self._error_threshold:
                state.state = CircuitBreakerState.OPEN
                state.opened_at = monotonic()
                state.cool_down = self._reset_timeout
                state.consecutive_error_count = 0
                state.consecutive_success_count = 0
                state.half_open_admit = 0

        return _snapshot_of(state)

    async def transition(
        self,
        *,
        desired: CircuitBreakerState,
        cool_down: float | None = None,
    ) -> None:
        """Manual transition. Last-write-wins."""
        state = self._get_or_create()
        if desired == CircuitBreakerState.OPEN:
            state.state = CircuitBreakerState.OPEN
            state.opened_at = monotonic()
            state.cool_down = (
                cool_down if cool_down is not None else self._reset_timeout
            )
        else:
            state.state = desired
            state.opened_at = 0.0
            state.cool_down = 0.0
        state.consecutive_error_count = 0
        state.consecutive_success_count = 0
        state.half_open_admit = 0

    async def get_snapshot(self) -> CircuitBreakerSnapshot:
        """Read the current snapshot without mutating state."""
        state = self._states.get(self._name)
        if state is None:
            return _DEFAULT_SNAPSHOT
        return _snapshot_of(state)


def _snapshot_of(state: _BreakerState) -> CircuitBreakerSnapshot:
    return CircuitBreakerSnapshot(
        state=state.state,
        opened_at=state.opened_at,
        consecutive_error_count=state.consecutive_error_count,
        consecutive_success_count=state.consecutive_success_count,
    )


_DEFAULT_SNAPSHOT = CircuitBreakerSnapshot(
    state=CircuitBreakerState.CLOSED,
    opened_at=0.0,
    consecutive_error_count=0,
    consecutive_success_count=0,
)
