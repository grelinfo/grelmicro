"""Helpers to drive a background loop on a `VirtualClock`."""

import asyncio

from grelmicro.clock import VirtualClock

JITTER_CEILING = 1.2
"""Largest factor a jittered interval reaches, for a 20 percent jitter."""


async def until_asleep(clock: VirtualClock) -> None:
    """Return once a task sleeps on `clock`.

    A loop that sleeps again has finished the pass it was running, I/O
    included. The real-time poll only waits for that I/O, never for an
    interval.
    """
    while not clock._waiters:  # noqa: ASYNC110
        await asyncio.sleep(0.001)


async def run_passes(
    clock: VirtualClock, interval: float, passes: int = 1
) -> None:
    """Move `clock` past `passes` jittered intervals, one pass at a time.

    Each step waits until the loop sleeps again, so every pass has
    finished when this returns.
    """
    for _ in range(passes):
        await until_asleep(clock)
        await clock.advance(interval * JITTER_CEILING)
        await until_asleep(clock)
