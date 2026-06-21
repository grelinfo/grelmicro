"""Exact arithmetic tests for the adaptive-gate CUBIC controller.

The broader suite checks that the gate enables, shrinks, and recovers. These
tests pin the exact CUBIC values (`k`, `w_max`, the growth curve) and the
measured-rate computation, so a flipped operator or exponent in the rate math
is caught.
"""

from __future__ import annotations

import pytest

from grelmicro.resilience.shield import _adaptive_gate as gate_module
from grelmicro.resilience.shield._adaptive_gate import _AdaptiveGate
from tests.resilience.shield.conftest import _FakeClock

# CUBIC algorithm invariants (must match the source).
_C = 0.4
_BETA = 0.7
_THIRD = 1.0 / 3.0

_INIT_RATE = 100.0
_FLOOR = 0.1
_BIG_CAPACITY = 1000.0
_SUCCESS_T = 10.0
_CLOCK_START = 10.0


def _gate(clock: _FakeClock) -> _AdaptiveGate:
    return _AdaptiveGate(
        initial_max_rate=_INIT_RATE,
        capacity=_BIG_CAPACITY,
        min_rate_floor=_FLOOR,
        max_rate_cap=None,
        time_source=clock,
    )


def test_slow_down_sets_cubic_k_w_max_and_shrinks() -> None:
    """First slow-down records w_max, computes k, and shrinks by beta."""
    gate = _gate(_FakeClock(start=0.0))

    gate.on_slow_down()

    expected_k = ((_INIT_RATE * (1.0 - _BETA)) / _C) ** _THIRD
    assert gate.w_max == _INIT_RATE
    assert gate.k == pytest.approx(expected_k)
    assert gate.max_rate == pytest.approx(_INIT_RATE * _BETA)


def test_on_success_follows_cubic_growth_curve() -> None:
    """A success recomputes the rate as `C * (t - last_fail - k)^3 + w_max`."""
    clock = _FakeClock(start=0.0)
    gate = _gate(clock)
    gate.on_slow_down()  # enables, last_fail = 0, w_max = initial

    clock.advance(_SUCCESS_T)
    gate.on_success()

    k = ((_INIT_RATE * (1.0 - _BETA)) / _C) ** _THIRD
    offset = _SUCCESS_T - 0.0 - k
    expected = _C * offset**3 + _INIT_RATE
    assert gate.max_rate == pytest.approx(expected)


async def test_measured_rate_is_span_over_samples() -> None:
    """Measured rate is `(samples - 1) / (newest - oldest)`."""
    clock = _FakeClock(start=_CLOCK_START)
    gate = _gate(clock)

    await gate.acquire()  # disabled: records t=10
    clock.advance(1.0)
    await gate.acquire()  # records t=11
    clock.advance(1.0)
    await gate.acquire()  # records t=12

    # (3 - 1) / (12 - 10) = 1.0
    assert gate.measured_rate() == pytest.approx(1.0)


async def test_measured_rate_needs_two_samples() -> None:
    """One sample is not enough; two samples give a finite rate."""
    clock = _FakeClock(start=_CLOCK_START)
    gate = _gate(clock)

    await gate.acquire()  # one sample
    assert gate.measured_rate() == float("inf")

    clock.advance(2.0)
    await gate.acquire()  # two samples spanning 2.0s
    assert gate.measured_rate() == pytest.approx(0.5)


_REFILL_RATE = 2.0
_REFILL_ELAPSED = 3.0
_START_TOKENS = 5.0


def test_refill_adds_rate_times_elapsed() -> None:
    """Refill adds `elapsed * max_rate` tokens, not `elapsed / max_rate`."""
    clock = _FakeClock(start=_CLOCK_START)
    gate = _gate(clock)
    gate._tokens = 0.0
    gate._updated_at = _CLOCK_START
    gate._max_rate = _REFILL_RATE

    clock.advance(_REFILL_ELAPSED)
    gate._refill(clock())

    # elapsed = (start + 3) - start = 3; tokens = 0 + 3 * 2 = 6
    assert gate._tokens == _REFILL_ELAPSED * _REFILL_RATE


async def test_acquire_deducts_one_token_when_available() -> None:
    """An acquire with a full bucket removes exactly one token."""
    clock = _FakeClock(start=0.0)
    gate = _gate(clock)
    gate.on_slow_down()  # enable the gate
    gate._tokens = _START_TOKENS
    gate._updated_at = 0.0
    gate._max_rate = 1.0  # refill at elapsed 0 adds nothing

    await gate.acquire()

    assert gate._tokens == _START_TOKENS - 1.0


async def test_acquire_wait_is_deficit_over_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked acquire waits `(1 - tokens) / max_rate` seconds."""
    clock = _FakeClock(start=0.0)
    gate = _gate(clock)
    gate.on_slow_down()
    gate._tokens = 0.4
    gate._updated_at = 0.0
    gate._max_rate = _REFILL_RATE

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)  # let the next refill complete the token

    monkeypatch.setattr(gate_module, "sleep", fake_sleep)

    await gate.acquire()

    # (1.0 - 0.4) / 2.0 = 0.3
    assert waits[0] == pytest.approx((1.0 - 0.4) / _REFILL_RATE)
