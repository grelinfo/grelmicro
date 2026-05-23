"""Property-based tests for the exponential backoff strategy.

Hypothesis explores random `(base_delay, max_delay, attempt)`
combinations across every jitter mode and checks the documented
bounds hold.
"""

from __future__ import annotations

import random
from itertools import pairwise

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from grelmicro.resilience.backoffs.exponential import (
    ExponentialBackoff,
    _ExponentialStrategy,
)

pytestmark = [pytest.mark.timeout(5)]


_BASES = st.floats(
    min_value=1e-4, max_value=1.0, allow_nan=False, allow_infinity=False
)
_MAX_DELAYS = st.floats(
    min_value=1.0, max_value=60.0, allow_nan=False, allow_infinity=False
)
_ATTEMPTS = st.integers(min_value=1, max_value=30)


def _raw(base: float, cap: float, attempt: int) -> float:
    return min(base * (2 ** (attempt - 1)), cap)


@given(base=_BASES, cap=_MAX_DELAYS, attempt=_ATTEMPTS)
@settings(max_examples=200, deadline=None)
def test_no_jitter_matches_formula(
    base: float, cap: float, attempt: int
) -> None:
    """Without jitter, delay equals the documented closed form."""
    config = ExponentialBackoff(base_delay=base, max_delay=cap, jitter="none")
    strategy = _ExponentialStrategy(config)
    assert strategy.delay(attempt) == _raw(base, cap, attempt)


@given(base=_BASES, cap=_MAX_DELAYS, attempt=_ATTEMPTS)
@settings(max_examples=200, deadline=None)
def test_full_jitter_within_zero_and_raw(
    base: float, cap: float, attempt: int
) -> None:
    """`full` jitter samples from `[0, raw]`."""
    config = ExponentialBackoff(base_delay=base, max_delay=cap, jitter="full")
    strategy = _ExponentialStrategy(config)
    random.seed(0)
    delay = strategy.delay(attempt)
    assert 0.0 <= delay <= _raw(base, cap, attempt)


@given(base=_BASES, cap=_MAX_DELAYS, attempt=_ATTEMPTS)
@settings(max_examples=200, deadline=None)
def test_equal_jitter_within_half_raw_and_raw(
    base: float, cap: float, attempt: int
) -> None:
    """`equal` jitter samples from `[raw/2, raw]`."""
    config = ExponentialBackoff(base_delay=base, max_delay=cap, jitter="equal")
    strategy = _ExponentialStrategy(config)
    random.seed(0)
    delay = strategy.delay(attempt)
    raw = _raw(base, cap, attempt)
    assert raw / 2 <= delay <= raw


@given(
    base=_BASES,
    cap=_MAX_DELAYS,
    attempts=st.lists(_ATTEMPTS, min_size=1, max_size=10),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_decorrelated_jitter_stays_in_bounds(
    base: float, cap: float, attempts: list[int]
) -> None:
    """`decorrelated` jitter stays in `[base_delay, max_delay]`."""
    config = ExponentialBackoff(
        base_delay=base, max_delay=cap, jitter="decorrelated"
    )
    strategy = _ExponentialStrategy(config)
    random.seed(0)
    for attempt in attempts:
        delay = strategy.delay(attempt)
        assert base <= delay <= cap


@given(
    base=_BASES,
    cap=_MAX_DELAYS,
    attempts=st.lists(_ATTEMPTS, min_size=2, max_size=10),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_no_jitter_raw_is_non_decreasing_with_attempt(
    base: float, cap: float, attempts: list[int]
) -> None:
    """For `jitter=none`, `delay(N+1) >= delay(N)` when both are below cap."""
    config = ExponentialBackoff(base_delay=base, max_delay=cap, jitter="none")
    strategy = _ExponentialStrategy(config)
    delays = [strategy.delay(n) for n in sorted(attempts)]
    for a, b in pairwise(delays):
        assert b >= a
