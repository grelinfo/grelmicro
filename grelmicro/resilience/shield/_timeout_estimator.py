"""Per-attempt timeout estimator.

Tracks the last 32 successful latencies in a power-of-two ring and
returns the p95 multiplied by 2.5, clamped to the configured range.
"""

from __future__ import annotations

import math

__all__ = ["_TimeoutEstimator"]

_WINDOW: int = 32
_P95_MULTIPLIER: float = 2.5


class _TimeoutEstimator:
    """Rolling p95 timeout estimator.

    Records successful latencies in a 32-slot ring. Each `estimate`
    call computes p95 of the recorded samples, multiplies by 2.5, and
    clamps to `[clamp_min, clamp_max]`. Returns `initial_timeout` when
    no samples have been recorded yet.
    """

    __slots__ = (
        "_buffer",
        "_clamp_max",
        "_clamp_min",
        "_count",
        "_index",
        "_initial",
    )

    def __init__(
        self,
        *,
        initial_timeout: float,
        clamp_min: float,
        clamp_max: float,
    ) -> None:
        """Initialize an empty estimator."""
        if clamp_min <= 0 or clamp_max <= 0:
            msg = "clamp bounds must be positive"
            raise ValueError(msg)
        if clamp_min > clamp_max:
            msg = f"clamp_min ({clamp_min}) must be <= clamp_max ({clamp_max})"
            raise ValueError(msg)
        self._initial = initial_timeout
        self._clamp_min = clamp_min
        self._clamp_max = clamp_max
        self._buffer: list[float] = [0.0] * _WINDOW
        self._index = 0
        self._count = 0

    def record(self, latency: float) -> None:
        """Record one successful latency in seconds."""
        if latency < 0 or not math.isfinite(latency):
            return
        self._buffer[self._index] = latency
        self._index = (self._index + 1) % _WINDOW
        if self._count < _WINDOW:
            self._count += 1

    def estimate(self) -> float:
        """Return the per-attempt timeout in seconds for the next call."""
        if self._count == 0:
            return self._clamp(self._initial)
        samples = sorted(self._buffer[: self._count])
        # p95: the smallest value v such that 95% of samples <= v.
        # Use the nearest-rank index: ceil(0.95 * n) - 1, clamped to range.
        rank = max(0, math.ceil(0.95 * self._count) - 1)
        rank = min(rank, self._count - 1)
        p95 = samples[rank]
        return self._clamp(p95 * _P95_MULTIPLIER)

    def _clamp(self, value: float) -> float:
        """Clamp `value` to `[clamp_min, clamp_max]`."""
        if value < self._clamp_min:
            return self._clamp_min
        if value > self._clamp_max:
            return self._clamp_max
        return value
