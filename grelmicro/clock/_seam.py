"""Active-clock seam used by time-dependent primitives.

Primitives call `monotonic()` and `sleep()` from here instead of `time` and
`asyncio` directly. When no clock is installed (the production default), these
forward straight to `time.monotonic` and `asyncio.sleep`, so there is no
behavior change and the only cost is one `ContextVar` read. When a `Clock` is
active (a `VirtualClock` in a test), they route through it.
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grelmicro.clock.abc import ClockBackend

_active_clock: ContextVar[ClockBackend | None] = ContextVar(
    "grelmicro_active_clock", default=None
)


def monotonic() -> float:
    """Return the active clock's monotonic time, or `time.monotonic()`."""
    clock = _active_clock.get()
    if clock is None:
        return time.monotonic()
    return clock.monotonic()


async def sleep(seconds: float) -> None:
    """Sleep on the active clock, or `asyncio.sleep()` when none is installed."""
    clock = _active_clock.get()
    if clock is None:
        await asyncio.sleep(seconds)
        return
    await clock.sleep(seconds)
