"""Strategy-level snapshot tests for the in-memory circuit breaker.

The broader suite asserts the observable open/closed state. These tests pin
the exact snapshot fields (`opened_at`, `consecutive_error_count`,
`consecutive_success_count`) and the bookkeeping that drives them across every
transition, plus per-name state isolation and half-open admission capacity.
They drive the strategy directly through a `VirtualClock` so the cool-down
timing is deterministic.
"""

import pytest

from grelmicro.clock import VirtualClock
from grelmicro.resilience import ConsecutiveCountConfig
from grelmicro.resilience._protocol import CircuitBreakerStrategy
from grelmicro.resilience.circuitbreaker import CircuitBreakerState
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)

pytestmark = [pytest.mark.timeout(1)]

CLOSED = CircuitBreakerState.CLOSED
OPEN = CircuitBreakerState.OPEN
HALF_OPEN = CircuitBreakerState.HALF_OPEN
CLOCK_START = 1000.0


def strategy(
    config: ConsecutiveCountConfig, *, name: str = "cb"
) -> CircuitBreakerStrategy:
    """Bind a fresh in-memory strategy for the given config."""
    return MemoryCircuitBreakerAdapter().bind(name=name, config=config)


async def test_error_threshold_opens_and_resets_counters() -> None:
    """Reaching the error threshold opens the breaker and stamps opened_at."""
    config = ConsecutiveCountConfig(error_threshold=2, reset_timeout=30.0)
    async with VirtualClock(start=CLOCK_START):
        cb = strategy(config)

        snap = await cb.record_outcome(success=False)
        assert snap.state is CLOSED
        assert snap.consecutive_error_count == 1
        assert snap.consecutive_success_count == 0
        assert snap.opened_at == 0.0

        snap = await cb.record_outcome(success=False)  # hits threshold
        assert snap.state is OPEN
        assert snap.opened_at == CLOCK_START
        assert snap.consecutive_error_count == 0
        assert snap.consecutive_success_count == 0


async def test_half_open_entry_and_close_reset_snapshot() -> None:
    """OPEN to HALF_OPEN to CLOSED clears opened_at and both counters."""
    config = ConsecutiveCountConfig(
        error_threshold=1,
        success_threshold=2,
        reset_timeout=30.0,
        half_open_capacity=5,
    )
    async with VirtualClock(start=CLOCK_START) as clock:
        cb = strategy(config)

        await cb.record_outcome(success=False)  # opens, opened_at=1000
        assert await cb.try_acquire() is False  # before cool-down elapses

        await clock.advance(30.0)
        assert await cb.try_acquire() is True  # OPEN -> HALF_OPEN, admits one

        snap = await cb.get_snapshot()
        assert snap.state is HALF_OPEN
        assert snap.opened_at == 0.0
        assert snap.consecutive_error_count == 0
        assert snap.consecutive_success_count == 0

        snap = await cb.record_outcome(success=True)
        assert snap.state is HALF_OPEN
        assert snap.consecutive_success_count == 1
        assert snap.consecutive_error_count == 0

        snap = await cb.record_outcome(success=True)  # second success closes
        assert snap.state is CLOSED
        assert snap.opened_at == 0.0
        assert snap.consecutive_success_count == 0
        assert snap.consecutive_error_count == 0


async def test_open_cool_down_governs_half_open_timing() -> None:
    """The cool-down stamped on open decides when half-open is allowed."""
    config = ConsecutiveCountConfig(error_threshold=1, reset_timeout=30.0)
    async with VirtualClock(start=CLOCK_START) as clock:
        cb = strategy(config)

        await cb.record_outcome(success=False)  # opens, cool_down=30
        await clock.advance(5.0)
        assert await cb.try_acquire() is False  # 5s < 30s, still open

        await clock.advance(25.0)  # 30s total
        assert await cb.try_acquire() is True  # cool-down elapsed


async def test_manual_transition_sets_and_clears_opened_at() -> None:
    """Manual open stamps opened_at, any other target clears it."""
    config = ConsecutiveCountConfig(error_threshold=5, reset_timeout=30.0)
    async with VirtualClock(start=CLOCK_START):
        cb = strategy(config)

        await cb.transition(desired=OPEN)
        snap = await cb.get_snapshot()
        assert snap.state is OPEN
        assert snap.opened_at == CLOCK_START

        await cb.transition(desired=CLOSED)
        snap = await cb.get_snapshot()
        assert snap.state is CLOSED
        assert snap.opened_at == 0.0


async def test_state_is_isolated_per_name() -> None:
    """Two breakers on one adapter keep independent state per name."""
    config = ConsecutiveCountConfig(error_threshold=1, reset_timeout=30.0)
    backend = MemoryCircuitBreakerAdapter()
    async with VirtualClock(start=CLOCK_START):
        first = backend.bind(name="first", config=config)
        second = backend.bind(name="second", config=config)

        await first.record_outcome(success=False)  # opens "first" only

        assert (await first.get_snapshot()).state is OPEN
        assert (await second.get_snapshot()).state is CLOSED


async def test_half_open_admission_capacity_and_release() -> None:
    """Half-open admits up to capacity, and an outcome frees one slot."""
    config = ConsecutiveCountConfig(
        error_threshold=5,
        success_threshold=10,
        reset_timeout=30.0,
        half_open_capacity=2,
    )
    async with VirtualClock(start=CLOCK_START) as clock:
        cb = strategy(config)

        for _ in range(5):
            await cb.record_outcome(success=False)  # opens
        await clock.advance(30.0)

        assert await cb.try_acquire() is True  # admit 1
        assert await cb.try_acquire() is True  # admit 2
        assert await cb.try_acquire() is False  # at capacity

        # A recorded success in half-open releases exactly one slot.
        await cb.record_outcome(success=True)
        assert await cb.try_acquire() is True  # one slot freed
        assert await cb.try_acquire() is False  # full again

        # A recorded error in half-open also releases exactly one slot.
        await cb.record_outcome(success=False)
        assert await cb.try_acquire() is True
        assert await cb.try_acquire() is False
