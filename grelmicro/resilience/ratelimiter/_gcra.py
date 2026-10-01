"""Sliding window (GCRA) arithmetic in whole microseconds.

Every time is an integer count of microseconds, so each comparison is
exact. The Redis script and the Postgres functions compute the same
steps.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import NamedTuple

from grelmicro.resilience._protocol import RateLimitResult

MICROSECONDS = 1_000_000
"""Microseconds in a second."""

_ONE_MICROSECOND = timedelta(microseconds=1)

MAX_WINDOW = timedelta(days=36_500)
"""Longest sliding window, a hundred years."""


def to_microseconds(seconds: float) -> int:
    """Return the clock reading `seconds` as whole microseconds, floored."""
    return math.floor(seconds * MICROSECONDS)


def window_microseconds(window: int | timedelta) -> int:
    """Return `window`, whole seconds or a timedelta, in microseconds."""
    if isinstance(window, timedelta):
        return window // _ONE_MICROSECOND
    return window * MICROSECONDS


def emission_interval(window: int | timedelta, limit: int) -> int:
    """Return the microseconds one request spends, truncated."""
    return window_microseconds(window) // limit


class Decision(NamedTuple):
    """What an acquire decided, and the arrival time to store when admitted."""

    result: RateLimitResult
    tat: int | None


def acquire(
    *, tat: int, now: int, cost: int, limit: int, emission: int
) -> Decision:
    """Spend `cost` requests at `now` against the stored arrival time `tat`."""
    gap = max(0, tat - now)
    reset = gap + emission * cost
    diff = emission * limit - reset
    if diff < 0:
        return Decision(
            RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=0,
                retry_after=-diff / MICROSECONDS,
                reset_after=gap / MICROSECONDS,
            ),
            None,
        )
    return Decision(
        RateLimitResult(
            allowed=True,
            limit=limit,
            remaining=diff // emission,
            retry_after=0.0,
            reset_after=reset / MICROSECONDS,
        ),
        now + reset,
    )


def peek(*, tat: int, now: int, limit: int, emission: int) -> RateLimitResult:
    """Return what a one-request acquire at `now` would decide, spending nothing."""
    gap = max(0, tat - now)
    diff = emission * limit - gap
    remaining = diff // emission
    if remaining <= 0:
        return RateLimitResult(
            allowed=False,
            limit=limit,
            remaining=0,
            retry_after=(emission - diff) / MICROSECONDS,
            reset_after=gap / MICROSECONDS,
        )
    return RateLimitResult(
        allowed=True,
        limit=limit,
        remaining=remaining,
        retry_after=0.0,
        reset_after=gap / MICROSECONDS,
    )
