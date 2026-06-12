"""Tests for the clock abstraction."""

import asyncio
import time

import pytest

from grelmicro.clock import (
    ClockBackend,
    RealClock,
    VirtualClock,
    monotonic,
    sleep,
)

pytestmark = [pytest.mark.timeout(1)]

_TOLERANCE = 0.1
_SHORT = 0.01


def test_backends_satisfy_protocol() -> None:
    """Both clocks are runtime `ClockBackend` instances."""
    assert isinstance(RealClock(), ClockBackend)
    assert isinstance(VirtualClock(), ClockBackend)


def test_clocks_are_components() -> None:
    """Clocks register as components of kind `clock`."""
    assert RealClock().kind == "clock"
    assert VirtualClock(name="test").name == "test"


def test_real_clock_name() -> None:
    """`RealClock` exposes its registration name."""
    assert RealClock().name == "default"
    assert RealClock(name="wall").name == "wall"


async def test_seam_defaults_to_real_time() -> None:
    """With no clock installed the seam uses real time."""
    assert abs(monotonic() - time.monotonic()) < _TOLERANCE
    start = time.monotonic()
    await sleep(0.01)
    assert time.monotonic() - start >= _SHORT


async def test_real_clock_installs_and_restores() -> None:
    """`RealClock` routes the seam to real time and restores on exit."""
    async with RealClock():
        assert abs(monotonic() - time.monotonic()) < _TOLERANCE
        await sleep(0)
    assert abs(monotonic() - time.monotonic()) < _TOLERANCE


async def test_virtual_clock_starts_at_configured_time() -> None:
    """`monotonic()` reflects the virtual start value."""
    start = 100.0
    async with VirtualClock(start=start):
        assert monotonic() == start


async def test_virtual_clock_advance_wakes_due_sleeper() -> None:
    """A sleeper wakes only once the clock passes its deadline."""
    woke: list[float] = []

    async with VirtualClock() as clock:

        async def sleeper() -> None:
            await sleep(30)
            woke.append(monotonic())

        task = asyncio.create_task(sleeper())
        await asyncio.sleep(0)
        await clock.advance(10)
        assert woke == []
        await clock.advance(25)
        assert woke == [35.0]
        await task


async def test_virtual_clock_zero_sleep_returns_immediately() -> None:
    """A non-positive sleep just yields without registering a waiter."""
    async with VirtualClock() as clock:
        await sleep(0)
        await sleep(-1)
        assert clock._waiters == []


async def test_virtual_clock_advance_backwards_raises() -> None:
    """The clock cannot move backwards."""
    async with VirtualClock() as clock:
        with pytest.raises(ValueError, match="backwards"):
            await clock.advance(-1)


async def test_virtual_clock_cancelled_sleep_drops_waiter() -> None:
    """Cancelling a sleeping task removes its waiter."""
    async with VirtualClock() as clock:
        task = asyncio.create_task(sleep(30))
        await asyncio.sleep(0)
        assert len(clock._waiters) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clock._waiters == []


async def test_virtual_clock_advance_skips_already_cancelled_waiter() -> None:
    """Advancing past a just-cancelled sleeper drops it without erroring."""
    async with VirtualClock() as clock:
        task = asyncio.create_task(sleep(30))
        await asyncio.sleep(0)
        task.cancel()  # cancels the waiter future before its cleanup runs
        await clock.advance(30)
        assert clock._waiters == []
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_virtual_clock_independent_deadlines() -> None:
    """Each sleeper wakes at its own deadline."""
    woke: list[str] = []

    async with VirtualClock() as clock:

        async def sleeper(label: str, seconds: float) -> None:
            await sleep(seconds)
            woke.append(label)

        tasks = [
            asyncio.create_task(sleeper("a", 10)),
            asyncio.create_task(sleeper("b", 20)),
        ]
        await asyncio.sleep(0)
        await clock.advance(10)
        assert woke == ["a"]
        await clock.advance(10)
        assert woke == ["a", "b"]
        await asyncio.gather(*tasks)
