"""Per-attempt timeout estimator tests."""

from __future__ import annotations

import pytest

from grelmicro.resilience.shield._timeout_estimator import _TimeoutEstimator


def test_no_samples_returns_initial_clamped() -> None:
    """Without samples the estimator returns `initial_timeout` clamped."""
    est = _TimeoutEstimator(
        initial_timeout=2.0,
        clamp_min=0.5,
        clamp_max=10.0,
    )
    assert est.estimate() == 2.0  # noqa: PLR2004


def test_initial_value_below_floor_is_clamped() -> None:
    """An initial below `clamp_min` is raised to the floor."""
    est = _TimeoutEstimator(
        initial_timeout=0.001,
        clamp_min=0.5,
        clamp_max=10.0,
    )
    assert est.estimate() == 0.5  # noqa: PLR2004


def test_initial_value_above_ceiling_is_clamped() -> None:
    """An initial above `clamp_max` is dropped to the ceiling."""
    est = _TimeoutEstimator(
        initial_timeout=1_000.0,
        clamp_min=0.5,
        clamp_max=10.0,
    )
    assert est.estimate() == 10.0  # noqa: PLR2004


def test_p95_times_2_5_after_samples() -> None:
    """Estimate = p95 of samples * 2.5, clamped to `[min, max]`."""
    est = _TimeoutEstimator(
        initial_timeout=99.0,
        clamp_min=0.001,
        clamp_max=999.0,
    )
    for value in (0.1, 0.2, 0.3, 0.4, 0.5):
        est.record(value)
    # 5 samples -> nearest-rank p95 index = ceil(0.95 * 5) - 1 = 4 -> 0.5.
    assert est.estimate() == pytest.approx(0.5 * 2.5)


def test_ring_buffer_rolls_over_past_32_samples() -> None:
    """Only the last 32 samples shape the estimate."""
    est = _TimeoutEstimator(
        initial_timeout=99.0,
        clamp_min=0.001,
        clamp_max=999.0,
    )
    # Fill with very high latencies, then with low ones. The high
    # samples must be evicted by the rollover.
    for _ in range(40):
        est.record(100.0)
    for _ in range(32):
        est.record(0.1)
    assert est.estimate() == pytest.approx(0.1 * 2.5)


def test_record_ignores_negative_and_non_finite() -> None:
    """Bad inputs are silently dropped, the estimator stays empty."""
    est = _TimeoutEstimator(
        initial_timeout=2.0,
        clamp_min=0.5,
        clamp_max=10.0,
    )
    est.record(-1.0)
    est.record(float("inf"))
    est.record(float("nan"))
    assert est.estimate() == 2.0  # noqa: PLR2004


def test_clamp_bounds_are_validated() -> None:
    """Constructor rejects non-positive bounds and inverted ranges."""
    with pytest.raises(ValueError, match="clamp"):
        _TimeoutEstimator(
            initial_timeout=1.0,
            clamp_min=0.0,
            clamp_max=10.0,
        )
    with pytest.raises(ValueError, match="clamp_min"):
        _TimeoutEstimator(
            initial_timeout=1.0,
            clamp_min=10.0,
            clamp_max=1.0,
        )
