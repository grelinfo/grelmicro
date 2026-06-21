"""Exact-value adaptive-gate tests for boundary and formula survivors.

These pin behavior the existing exact tests miss because they start the
clock and last-fail at zero: the CUBIC offset subtracts last_fail (a
sign flip diverges only when last_fail is nonzero), the measured-rate
clamp multiplies, a sub-second elapsed still refills, the cap may equal
the floor, and enabling snaps the bucket to zero tokens.
"""

from __future__ import annotations

import pytest

from grelmicro.resilience.shield._adaptive_gate import _AdaptiveGate
from tests.resilience.shield.conftest import _FakeClock

_C = 0.4
_BETA = 0.7
_THIRD = 1.0 / 3.0

_INIT_RATE = 100.0
_FLOOR = 0.1
_BIG_CAPACITY = 1000.0
_START = 50.0


def _gate(clock: _FakeClock, *, max_rate_cap: float | None = None) -> _AdaptiveGate:
    return _AdaptiveGate(
        initial_max_rate=_INIT_RATE,
        capacity=_BIG_CAPACITY,
        min_rate_floor=_FLOOR,
        max_rate_cap=max_rate_cap,
        time_source=clock,
    )


def test_cap_equal_to_floor_is_allowed() -> None:
    """`max_rate_cap == min_rate_floor` is valid (the bound is `<`, not `<=`)."""
    gate = _AdaptiveGate(
        initial_max_rate=_INIT_RATE,
        capacity=_BIG_CAPACITY,
        min_rate_floor=_FLOOR,
        max_rate_cap=_FLOOR,
    )
    assert gate.max_rate == _INIT_RATE


def test_on_success_offset_subtracts_nonzero_last_fail() -> None:
    """The CUBIC offset is `now - last_fail - k` with a nonzero last_fail."""
    clock = _FakeClock(start=_START)
    gate = _gate(clock)
    gate.on_slow_down()  # last_fail = 50, w_max = initial

    clock.advance(10.0)  # now = 60
    gate.on_success()

    k = ((_INIT_RATE * (1.0 - _BETA)) / _C) ** _THIRD
    offset = 60.0 - _START - k  # a `+` flip would use 60 + 50 - k
    expected = _C * offset**3 + _INIT_RATE
    assert gate.max_rate == pytest.approx(expected)


async def test_measured_rate_finite_for_sub_second_span() -> None:
    """A span of 0.5 seconds yields a finite rate, not infinity."""
    clock = _FakeClock(start=_START)
    gate = _gate(clock)

    await gate.acquire()  # records t=50
    clock.advance(0.5)
    await gate.acquire()  # records t=50.5

    # (2 - 1) / 0.5 = 2.0; a `<= 1` guard would wrongly return inf.
    assert gate.measured_rate() == pytest.approx(2.0)


def test_refill_adds_tokens_for_sub_second_elapsed() -> None:
    """A sub-second elapsed still refills (the guard is `> 0`, not `> 1`)."""
    clock = _FakeClock(start=_START)
    gate = _gate(clock)
    gate._tokens = 0.0
    gate._updated_at = _START
    gate._max_rate = 2.0

    clock.advance(0.5)
    gate._refill(clock())

    assert gate._tokens == pytest.approx(0.5 * 2.0)


def test_apply_rate_multiplies_measured_clamp() -> None:
    """The measured-rate clamp is `1.5 * measured`, not `1.5 / measured`."""
    clock = _FakeClock(start=_START)
    gate = _gate(clock)
    gate.on_slow_down()  # enable

    # Two sends 0.5s apart give measured_rate = 2.0, so the clamp is
    # 1.5 * 2.0 = 3.0. A `/` flip would clamp at 1.5 / 2.0 = 0.75.
    gate._send_window.append(_START)
    gate._send_window.append(_START + 0.5)
    assert gate.measured_rate() == pytest.approx(2.0)

    clock.advance(100.0)  # huge offset so the CUBIC candidate is large
    gate.on_success()

    assert gate.max_rate == pytest.approx(3.0)


def test_first_slow_down_snaps_tokens_to_zero() -> None:
    """Enabling the gate snaps the token bucket to zero."""
    clock = _FakeClock(start=_START)
    gate = _gate(clock)
    gate._tokens = 5.0# accumulated while inert

    gate.on_slow_down()

    assert gate._tokens == 0.0


async def test_send_window_is_bounded_to_eight_samples() -> None:
    """The send window keeps at most the last 8 timestamps."""
    clock = _FakeClock(start=_START)
    gate = _gate(clock)

    for _ in range(12):
        await gate.acquire()  # disabled path records the timestamp
        clock.advance(1.0)

    assert len(gate._send_window) == 8  # noqa: PLR2004
