"""Clock-driven resilience tests.

Prove that the in-process resilience primitives route their time access
through grelmicro's clock seam. Under a `VirtualClock`, backoff sleeps and
breaker cool-downs are driven by `clock.advance(...)` with no real waiting.
"""

import asyncio

import pytest

from grelmicro import Grelmicro
from grelmicro.clock import VirtualClock
from grelmicro.resilience import CircuitBreakers, Retry
from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitBreakerState,
)
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)

_BACKOFF = 5.0
_RESET_TIMEOUT = 30.0
_EXPECTED_CALLS = 2

_BOOM = ValueError("boom")


@pytest.mark.timeout(1)
async def test_retry_backoff_driven_by_virtual_clock(
    clock: VirtualClock,
) -> None:
    """A retry backoff is advanced instantly, with no real sleep."""
    calls = 0

    @Retry.constant("clock_retry", when=ValueError, attempts=2, delay=_BACKOFF)
    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _BOOM
        return "ok"

    task = asyncio.create_task(flaky())  # ty: ignore[invalid-argument-type]

    # Let the first attempt run, fail, and suspend on the backoff sleep.
    await asyncio.sleep(0)
    assert calls == 1
    assert not task.done()

    # Advance virtual time past the backoff: the second attempt runs.
    await clock.advance(_BACKOFF)
    assert await task == "ok"
    assert calls == _EXPECTED_CALLS


@pytest.mark.timeout(1)
async def test_circuit_breaker_half_open_driven_by_virtual_clock(
    clock: VirtualClock,
) -> None:
    """An open breaker moves to half-open after the cool-down elapses."""
    backend = MemoryCircuitBreakerAdapter()
    async with Grelmicro(uses=[CircuitBreakers(backend)]):
        cb = CircuitBreaker.consecutive_count(
            "clock_cb",
            error_threshold=1,
            success_threshold=2,
            reset_timeout=_RESET_TIMEOUT,
        )

        # Trip the breaker open with one failure.
        with pytest.raises(ValueError, match="boom"):
            async with cb:
                raise _BOOM
        assert cb.state == CircuitBreakerState.OPEN

        # Before the cool-down elapses the breaker stays open.
        with pytest.raises(CircuitBreakerError):
            async with cb:
                pass
        assert cb.state == CircuitBreakerState.OPEN

        # Advance virtual time past the cool-down: the next admission
        # moves the breaker to half-open, no real waiting.
        await clock.advance(_RESET_TIMEOUT)
        async with cb:
            pass
        assert cb.state == CircuitBreakerState.HALF_OPEN
