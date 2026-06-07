"""Adaptive rate gate.

Token bucket with a CUBIC-style rate controller. Stays disabled until
the first slow-down signal arrives, then engages and self-tunes the
ceiling rate using the CUBIC curve.

CUBIC reference: RFC 9438. Constants `C = 0.4`, `beta = 0.7` are fixed
algorithm-level invariants, not tunable per profile.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from typing import TYPE_CHECKING

from grelmicro.clock import monotonic, sleep

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["_AdaptiveGate"]

_CUBIC_C: float = 0.4
_CUBIC_BETA: float = 0.7
_MEASURED_RATE_CLAMP: float = 1.5
_MEASURED_WINDOW: int = 8
_MIN_MEASURED_SAMPLES: int = 2


class _AdaptiveGate:
    """Token bucket gated by a CUBIC-style rate controller.

    Disabled at construction. Every `acquire` is a no-op until the
    first `on_slow_down` call. Once enabled, every `acquire` blocks
    until one token is available based on `max_rate`.

    On a successful response, `on_success` recomputes a candidate rate
    from the CUBIC growth curve. On a slow-down exception,
    `on_slow_down` records the new `w_max`, multiplies the current
    rate by `beta`, and recomputes `k`. The bucket's effective
    `max_rate` is clamped to `[min_rate_floor, min(candidate,
    1.5 * measured_rate, max_rate_cap)]`.
    """

    __slots__ = (
        "_capacity",
        "_enabled",
        "_initial_max_rate",
        "_k",
        "_last_fail",
        "_lock",
        "_max_rate",
        "_max_rate_cap",
        "_min_rate_floor",
        "_send_window",
        "_time",
        "_tokens",
        "_updated_at",
        "_w_max",
    )

    def __init__(
        self,
        *,
        initial_max_rate: float,
        capacity: float,
        min_rate_floor: float,
        max_rate_cap: float | None,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        """Initialize a disabled adaptive gate."""
        if initial_max_rate <= 0:
            msg = "initial_max_rate must be positive"
            raise ValueError(msg)
        if capacity <= 0:
            msg = "capacity must be positive"
            raise ValueError(msg)
        if min_rate_floor <= 0:
            msg = "min_rate_floor must be positive"
            raise ValueError(msg)
        if max_rate_cap is not None and max_rate_cap < min_rate_floor:
            msg = "max_rate_cap must be >= min_rate_floor"
            raise ValueError(msg)
        self._initial_max_rate = initial_max_rate
        self._max_rate = initial_max_rate
        self._capacity = capacity
        self._min_rate_floor = min_rate_floor
        self._max_rate_cap = max_rate_cap
        self._time = time_source or monotonic
        self._tokens = 0.0
        self._updated_at = self._time()
        self._enabled = False
        # CUBIC state.
        self._w_max = initial_max_rate
        self._k = 0.0
        self._last_fail = self._updated_at
        # Rolling send-timestamp window for measured-rate clamp.
        self._send_window: deque[float] = deque(maxlen=_MEASURED_WINDOW)
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Return True once the gate has observed a slow-down."""
        return self._enabled

    @property
    def max_rate(self) -> float:
        """Return the current ceiling rate in tokens per second."""
        return self._max_rate

    @property
    def w_max(self) -> float:
        """Return the CUBIC `w_max` (rate at the last slow-down)."""
        return self._w_max

    @property
    def k(self) -> float:
        """Return the CUBIC time offset `k` since the last slow-down."""
        return self._k

    @property
    def last_fail(self) -> float:
        """Return the timestamp of the most recent slow-down."""
        return self._last_fail

    def measured_rate(self) -> float:
        """Return the rolling outbound call rate in calls per second.

        Returns `math.inf` when the window holds fewer than 2 samples,
        which disables the measured-rate clamp until enough data is
        collected.
        """
        if len(self._send_window) < _MIN_MEASURED_SAMPLES:
            return math.inf
        oldest = self._send_window[0]
        newest = self._send_window[-1]
        elapsed = newest - oldest
        if elapsed <= 0:
            return math.inf
        return (len(self._send_window) - 1) / elapsed

    async def acquire(self) -> None:
        """Acquire one token. No-op while disabled.

        Blocks until one token is available based on the bucket fill
        rate. Refills passively on every state access.
        """
        if not self._enabled:
            # Still record the send timestamp so `measured_rate` reflects
            # the disabled-state throughput when CUBIC eventually engages.
            self._send_window.append(self._time())
            return
        while True:
            async with self._lock:
                now = self._time()
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._send_window.append(now)
                    return
                wait = (1.0 - self._tokens) / self._max_rate
            await sleep(wait)

    def _refill(self, now: float) -> None:
        """Refill the bucket based on elapsed time since the last access."""
        elapsed = now - self._updated_at
        if elapsed > 0:
            self._tokens = min(
                self._capacity, self._tokens + elapsed * self._max_rate
            )
            self._updated_at = now

    def on_success(self) -> None:
        """Update the CUBIC growth curve after a successful call.

        New candidate rate: `C * (t - last_fail - k)^3 + w_max`. The
        clamp `max(floor, min(candidate, 1.5 * measured, cap))` is
        applied to the bucket's effective `max_rate`.
        """
        if not self._enabled:
            return
        now = self._time()
        offset = now - self._last_fail - self._k
        candidate = _CUBIC_C * (offset**3) + self._w_max
        self._apply_rate(candidate, now)

    def on_slow_down(self) -> None:
        """Record a slow-down. Engages the gate on the first call.

        Sets `w_max` to the current rate, recomputes `k`, stamps
        `last_fail`, then shrinks the bucket rate to `current * beta`.
        """
        now = self._time()
        if not self._enabled:
            self._enabled = True
            # Snap the bucket so the new ceiling starts taking effect
            # immediately rather than carrying tokens accumulated while
            # the gate was inert.
            self._tokens = 0.0
            self._updated_at = now
        self._w_max = self._max_rate
        # k such that the success curve passes through w_max at t = k.
        # k = ((w_max * (1 - beta)) / C)^(1/3).
        self._k = ((self._w_max * (1.0 - _CUBIC_BETA)) / _CUBIC_C) ** (
            1.0 / 3.0
        )
        self._last_fail = now
        candidate = self._max_rate * _CUBIC_BETA
        self._apply_rate(candidate, now)

    def _apply_rate(self, candidate: float, now: float) -> None:
        """Clamp `candidate` and publish it as the new `max_rate`."""
        ceiling = candidate
        measured = self.measured_rate()
        if math.isfinite(measured):
            ceiling = min(ceiling, _MEASURED_RATE_CLAMP * measured)
        if self._max_rate_cap is not None:
            ceiling = min(ceiling, self._max_rate_cap)
        new_rate = max(self._min_rate_floor, ceiling)
        # Refill against the old rate before swapping, so already-earned
        # tokens are not dropped on the floor.
        self._refill(now)
        self._max_rate = new_rate
