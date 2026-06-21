"""Exact boundary tests for the shield per-attempt timeout estimator.

The broader suite checks clamping and the no-sample path. These tests pin the
validation boundaries and the p95 nearest-rank index, so an off-by-one in the
rank or a flipped comparison in the constructor is caught.
"""

from __future__ import annotations

from grelmicro.resilience.shield._timeout_estimator import _TimeoutEstimator

_INITIAL = 2.0
_LOW_MIN = 0.5
_WIDE_MAX = 1000.0
_P95_MULTIPLIER = 2.5
_RANK_SAMPLES = 20


def test_equal_clamp_bounds_are_allowed() -> None:
    """`clamp_min == clamp_max` is a valid (degenerate) range."""
    est = _TimeoutEstimator(
        initial_timeout=_INITIAL, clamp_min=_INITIAL, clamp_max=_INITIAL
    )
    assert est.estimate() == _INITIAL


def test_zero_latency_is_recorded() -> None:
    """A zero latency is a valid sample, not skipped."""
    est = _TimeoutEstimator(
        initial_timeout=_INITIAL, clamp_min=_LOW_MIN, clamp_max=_WIDE_MAX
    )
    est.record(0.0)
    # One recorded sample of 0.0 gives p95 = 0.0, clamped up to the floor,
    # rather than the unrecorded path that would return the initial.
    assert est.estimate() == _LOW_MIN


def test_negative_latency_is_skipped() -> None:
    """A negative latency is rejected, leaving the no-sample path."""
    est = _TimeoutEstimator(
        initial_timeout=_INITIAL, clamp_min=_LOW_MIN, clamp_max=_WIDE_MAX
    )
    est.record(-1.0)
    assert est.estimate() == _INITIAL


def test_p95_uses_nearest_rank_index() -> None:
    """p95 of 20 samples picks the 19th value (nearest-rank), times 2.5."""
    est = _TimeoutEstimator(
        initial_timeout=_INITIAL, clamp_min=_LOW_MIN, clamp_max=_WIDE_MAX
    )
    for value in range(1, _RANK_SAMPLES + 1):
        est.record(float(value))
    # rank = ceil(0.95 * 20) - 1 = 18 -> samples[18] = 19.0
    assert est.estimate() == 19.0 * _P95_MULTIPLIER
