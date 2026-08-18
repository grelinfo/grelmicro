"""Memory circuit-breaker adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from grelmicro.clock import monotonic
from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    CircuitBreakerSnapshot,
    CircuitBreakerStrategy,
)
from grelmicro.resilience.circuitbreaker import (
    _STATE_TTL,
    CircuitBreakerState,
    _resolve_state_ttl,
)

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.resilience.circuitbreaker.consecutive_count import (
        ConsecutiveCountConfig,
    )
    from grelmicro.types import BackendScope


_CULL_LIMIT = 32
"""Expired entries reclaimed per write, bounding the sweep's cost."""

_FORCED_STATES = (
    CircuitBreakerState.FORCED_OPEN,
    CircuitBreakerState.FORCED_CLOSED,
)
"""States held by an operator, which no lifetime may release."""

_NEVER = float("inf")
"""Deadline of an entry no lifetime may reclaim."""


@dataclass(slots=True)
class _BreakerState:
    """Per-breaker mutable state held by `MemoryCircuitBreakerAdapter`."""

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    opened_at: float = 0.0
    cool_down: float = 0.0
    consecutive_error_count: int = 0
    consecutive_success_count: int = 0
    half_open_admit: int = 0
    expires_at: float = 0.0
    """Monotonic deadline past which the entry reads as absent.

    Every write stamps it from the entry's own cool-down, so reading it
    is one float comparison and a sweep never judges another breaker's
    entry by its own configuration.
    """


class MemoryCircuitBreakerAdapter(CircuitBreakerBackend):
    """In-memory circuit breaker adapter.

    State for every breaker bound to this adapter is held in process,
    keyed by breaker name. Closing the adapter clears every breaker's
    state so the next start begins on a clean slate.

    Use it for tests and single-process deployments. Use
    `RedisCircuitBreakerAdapter` for fleet-wide shared state.
    """

    scope: ClassVar[BackendScope] = "process"
    """State lives in this process and is not shared beyond it."""

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

        Dispatches on the `config.kind` discriminator before reading any
        field, so an algorithm this adapter does not implement is refused
        rather than silently run as consecutive-count.
        """
        if config.kind != "consecutive_count":
            msg = f"Unsupported circuit breaker algorithm: {config.kind!r}"
            raise NotImplementedError(msg)
        return _MemoryConsecutiveCountStrategy(
            states=self._states,
            name=name,
            config=config,
            open_ttl=_resolve_state_ttl(config.reset_timeout),
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
        open_ttl: float,
    ) -> None:
        """Bind the strategy to the breaker's per-name state and config."""
        self._states = states
        self._name = name
        self._error_threshold = config.error_threshold
        self._success_threshold = config.success_threshold
        self._reset_timeout = config.reset_timeout
        self._half_open_capacity = config.half_open_capacity
        self._open_ttl = open_ttl

    def _live(self, now: float) -> _BreakerState:
        """Return the live entry for this breaker, creating it if needed.

        Expiry is lazy: an entry past its deadline reads as absent and
        is replaced, so a returning key starts from a clean `CLOSED`
        instead of inheriting counters nobody has touched for a day.

        A manually forced state carries no deadline. An operator holds
        it until an explicit `reset`, so a quiet period must not release
        the hold.
        """
        state = self._states.get(self._name)
        if state is None or now >= state.expires_at:
            state = _BreakerState(expires_at=now + _STATE_TTL)
            self._states[self._name] = state
            self._cull(now)
        return state

    def _cull(self, now: float) -> None:
        """Reclaim a bounded number of expired entries.

        Runs only when a new entry is added, and stops after
        `_CULL_LIMIT`, so no single call pays for the whole keyspace.
        """
        reclaimed = 0
        for name, state in list(self._states.items()):
            if reclaimed >= _CULL_LIMIT:
                return
            if name != self._name and now >= state.expires_at:
                del self._states[name]
                reclaimed += 1

    def _close(self) -> CircuitBreakerSnapshot:
        """Drop the entry and report the default snapshot.

        Every adapter reads a missing entry as `CLOSED`, so a circuit
        that closes with its counters cleared stores nothing at all.
        """
        self._states.pop(self._name, None)
        return _DEFAULT_SNAPSHOT

    async def try_acquire(self) -> bool:
        """Atomic admission in the loop thread.

        A missing entry reads as `CLOSED`, and a closed circuit admits
        whatever its counters say, so the steady-state path answers from
        one dictionary lookup without reading the clock.
        """
        state = self._states.get(self._name)
        if state is None:
            return True

        circuit = state.state
        if circuit in (
            CircuitBreakerState.CLOSED,
            CircuitBreakerState.FORCED_CLOSED,
        ):
            return True
        if circuit == CircuitBreakerState.FORCED_OPEN:
            return False

        return self._admit_recovering(state)

    def _admit_recovering(self, state: _BreakerState) -> bool:
        """Admit against a circuit that is neither closed nor forced.

        `try_acquire` has already answered every other state, so the
        entry here is `OPEN` or `HALF_OPEN`, and both need the clock.
        """
        now = monotonic()
        if now >= state.expires_at:
            self._live(now)
            return True

        if state.state == CircuitBreakerState.OPEN:
            if now < state.opened_at + state.cool_down:
                return False
            state.state = CircuitBreakerState.HALF_OPEN
            state.consecutive_error_count = 0
            state.consecutive_success_count = 0
            state.half_open_admit = 0
            state.opened_at = 0.0
            state.cool_down = 0.0
            state.expires_at = now + _STATE_TTL

        if state.half_open_admit < self._half_open_capacity:
            state.half_open_admit += 1
            state.expires_at = now + _STATE_TTL
            return True

        return False

    async def record_outcome(
        self,
        *,
        success: bool,
        duration: float = 0.0,  # noqa: ARG002
    ) -> CircuitBreakerSnapshot:
        """Record a call outcome and apply any state transition."""
        now = monotonic()
        state = self._live(now)

        if state.state in (
            CircuitBreakerState.FORCED_OPEN,
            CircuitBreakerState.FORCED_CLOSED,
            CircuitBreakerState.OPEN,
        ):
            return _snapshot_of(state)

        state.expires_at = now + _STATE_TTL

        if success:
            state.consecutive_success_count += 1
            state.consecutive_error_count = 0
            if state.state == CircuitBreakerState.HALF_OPEN:
                if state.half_open_admit > 0:  # pragma: no branch
                    state.half_open_admit -= 1
                if state.consecutive_success_count >= self._success_threshold:
                    return self._close()
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
                state.opened_at = now
                state.cool_down = self._reset_timeout
                state.expires_at = now + self._open_ttl
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
        """Manual transition. Last-write-wins.

        A transition to plain `CLOSED` clears every counter, which is
        exactly what a missing entry already means, so the entry is
        dropped instead of stored.
        """
        if desired == CircuitBreakerState.CLOSED:
            self._close()
            return
        now = monotonic()
        state = self._live(now)
        if desired == CircuitBreakerState.OPEN:
            state.state = CircuitBreakerState.OPEN
            state.opened_at = now
            state.cool_down = (
                cool_down if cool_down is not None else self._reset_timeout
            )
            state.expires_at = now + (
                self._open_ttl
                if cool_down is None
                else _resolve_state_ttl(cool_down)
            )
        else:
            state.state = desired
            state.opened_at = 0.0
            state.cool_down = 0.0
            state.expires_at = (
                _NEVER if desired in _FORCED_STATES else now + _STATE_TTL
            )
        state.consecutive_error_count = 0
        state.consecutive_success_count = 0
        state.half_open_admit = 0

    async def get_snapshot(self) -> CircuitBreakerSnapshot:
        """Read the current snapshot without mutating state."""
        state = self._states.get(self._name)
        if state is None or monotonic() >= state.expires_at:
            return _DEFAULT_SNAPSHOT
        return _snapshot_of(state)


def _snapshot_of(state: _BreakerState) -> CircuitBreakerSnapshot:
    return CircuitBreakerSnapshot(
        state=state.state,
        opened_at=state.opened_at,
        consecutive_error_count=state.consecutive_error_count,
        consecutive_success_count=state.consecutive_success_count,
        retry_after=_retry_after(state),
    )


def _retry_after(state: _BreakerState) -> float:
    """Return the seconds an `OPEN` circuit still has to wait out."""
    if state.state != CircuitBreakerState.OPEN:
        return 0.0
    return max(0.0, state.opened_at + state.cool_down - monotonic())


_DEFAULT_SNAPSHOT = CircuitBreakerSnapshot(
    state=CircuitBreakerState.CLOSED,
    opened_at=0.0,
    consecutive_error_count=0,
    consecutive_success_count=0,
)
