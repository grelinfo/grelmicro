"""Adaptive gate (token bucket + CUBIC) tests."""

from __future__ import annotations

import math

import pytest

from grelmicro.resilience.shield._adaptive_gate import _AdaptiveGate
from tests.resilience.shield.conftest import _FakeClock

_BETA = 0.7
_C = 0.4


async def test_disabled_acquire_is_a_noop() -> None:
    """Until the first slow-down, `acquire` returns immediately."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=10.0,
        capacity=10.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    assert gate.enabled is False
    await gate.acquire()
    await gate.acquire()
    assert gate.enabled is False


async def test_slow_down_enables_and_shrinks_rate() -> None:
    """First slow-down sets `max_rate` to `current * beta`."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=100.0,
        capacity=200.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    gate.on_slow_down()
    assert gate.enabled is True
    assert gate.w_max == 100.0  # noqa: PLR2004
    assert gate.max_rate == pytest.approx(100.0 * _BETA)


async def test_k_value_after_shrink_matches_formula() -> None:
    """`k = ((w_max * (1 - beta)) / C)^(1/3)`."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=50.0,
        capacity=50.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    gate.on_slow_down()
    expected_k = ((50.0 * (1.0 - _BETA)) / _C) ** (1.0 / 3.0)
    assert gate.k == pytest.approx(expected_k)


async def test_success_curve_grows_back_to_w_max_at_t_equals_k() -> None:
    """At `t = last_fail + k` the curve passes through `w_max`."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=50.0,
        capacity=50.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    gate.on_slow_down()
    # Advance the clock to exactly `last_fail + k` and make sure no
    # measured-rate clamp kicks in. The send window has been empty
    # since enabled, so `measured_rate` is `inf`.
    clock.advance(gate.k)
    gate.on_success()
    assert gate.max_rate == pytest.approx(50.0)


async def test_success_curve_value_at_arbitrary_offset() -> None:
    """`C * (offset)^3 + w_max` for `offset = t - last_fail - k`."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=50.0,
        capacity=50.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    gate.on_slow_down()
    extra = 1.0
    clock.advance(gate.k + extra)
    gate.on_success()
    expected = _C * (extra**3) + gate.w_max
    assert gate.max_rate == pytest.approx(expected)


async def test_min_rate_floor_is_enforced() -> None:
    """The bucket rate never drops below `min_rate_floor`."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=1.0,
        capacity=1.0,
        min_rate_floor=2.0,
        max_rate_cap=None,
        time_source=clock,
    )
    gate.on_slow_down()
    assert gate.max_rate == 2.0  # noqa: PLR2004


async def test_max_rate_cap_is_enforced_on_growth() -> None:
    """Set a hard ceiling and confirm CUBIC growth respects it."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=50.0,
        capacity=50.0,
        min_rate_floor=0.1,
        max_rate_cap=10.0,
        time_source=clock,
    )
    gate.on_slow_down()
    clock.advance(1000.0)
    gate.on_success()
    assert gate.max_rate <= 10.0  # noqa: PLR2004


async def test_measured_rate_returns_inf_with_fewer_than_two_samples() -> None:
    """The clamp disengages until at least two timestamps are observed."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=10.0,
        capacity=10.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    assert math.isinf(gate.measured_rate())


async def test_measured_rate_clamp_caps_success_growth() -> None:
    """Candidate rate is clamped to `1.5 * measured_rate`."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=50.0,
        capacity=50.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    # Build a measured rate of about 1/s while the gate is disabled.
    for _ in range(8):
        await gate.acquire()
        clock.advance(1.0)
    measured = gate.measured_rate()
    assert measured == pytest.approx(1.0, rel=0.2)
    gate.on_slow_down()
    clock.advance(1_000.0)
    gate.on_success()
    assert gate.max_rate <= 1.5 * measured + 1e-6


async def test_last_fail_getter_returns_timestamp() -> None:
    """`last_fail` exposes the most-recent slow-down timestamp."""
    clock = _FakeClock(start=100.0)
    gate = _AdaptiveGate(
        initial_max_rate=10.0,
        capacity=10.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    gate.on_slow_down()
    assert gate.last_fail == 100.0  # noqa: PLR2004


async def test_measured_rate_handles_zero_elapsed_window() -> None:
    """When all samples share the same timestamp, the clamp disengages."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=10.0,
        capacity=10.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    # Push several samples at the same timestamp.
    for _ in range(5):
        await gate.acquire()
    assert math.isinf(gate.measured_rate())


async def test_acquire_when_enabled_yields_under_full_bucket() -> None:
    """A bucket pre-filled past the threshold lets `acquire` proceed."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=10.0,
        capacity=10.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    gate.on_slow_down()
    # Advance the clock so the bucket refills to one whole token.
    clock.advance(10.0)
    await gate.acquire()


async def test_on_success_is_a_noop_while_disabled() -> None:
    """`on_success` does nothing until the gate has been enabled."""
    clock = _FakeClock()
    gate = _AdaptiveGate(
        initial_max_rate=50.0,
        capacity=50.0,
        min_rate_floor=0.1,
        max_rate_cap=None,
        time_source=clock,
    )
    gate.on_success()
    assert gate.enabled is False
    assert gate.max_rate == 50.0  # noqa: PLR2004


def test_capacity_must_be_positive() -> None:
    """Constructor validates inputs."""
    with pytest.raises(ValueError, match="initial_max_rate"):
        _AdaptiveGate(
            initial_max_rate=0,
            capacity=1,
            min_rate_floor=0.1,
            max_rate_cap=None,
        )
    with pytest.raises(ValueError, match="capacity"):
        _AdaptiveGate(
            initial_max_rate=1,
            capacity=0,
            min_rate_floor=0.1,
            max_rate_cap=None,
        )
    with pytest.raises(ValueError, match="min_rate_floor"):
        _AdaptiveGate(
            initial_max_rate=1,
            capacity=1,
            min_rate_floor=0,
            max_rate_cap=None,
        )
    with pytest.raises(ValueError, match="max_rate_cap"):
        _AdaptiveGate(
            initial_max_rate=1,
            capacity=1,
            min_rate_floor=1.0,
            max_rate_cap=0.5,
        )
