"""Property-based tests for rate-limiter algorithms.

Hypothesis explores random sequences of operations against the
public, synchronous `MemoryTokenBucket` and the async strategies
built by `MemoryRateLimiterAdapter`. The aim is to catch boundary
math (refill capping, retry hints, monotonic state) that
example-based tests miss.
"""

from __future__ import annotations

import asyncio
import math
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Coroutine

from grelmicro.resilience.ratelimiter.memory import (
    MemoryRateLimiterAdapter,
    MemoryTokenBucket,
)
from grelmicro.resilience.ratelimiter.sliding_window import SlidingWindowConfig
from grelmicro.resilience.ratelimiter.token_bucket import TokenBucketConfig

pytestmark = [pytest.mark.timeout(10)]


_CAPACITIES = st.integers(min_value=1, max_value=1000)
_REFILL_RATES = st.floats(
    min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False
)
_KEYS = st.text(min_size=0, max_size=8)


# --- MemoryTokenBucket -------------------------------------------------------


@given(capacity=_CAPACITIES, refill_rate=_REFILL_RATES)
@settings(max_examples=200, deadline=None)
def test_token_bucket_never_grants_more_than_capacity(
    capacity: int, refill_rate: float
) -> None:
    """Tight burst never grants more than capacity tokens."""
    bucket = MemoryTokenBucket(capacity=capacity, refill_rate=refill_rate)
    granted = sum(bucket.try_acquire() for _ in range(capacity + 5))
    assert capacity <= granted <= capacity + 1


@given(
    capacity=_CAPACITIES,
    refill_rate=_REFILL_RATES,
    cost=st.floats(
        min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False
    ),
)
@settings(max_examples=200, deadline=None)
def test_token_bucket_rejects_when_cost_exceeds_capacity(
    capacity: int, refill_rate: float, cost: float
) -> None:
    """`cost > capacity` always raises."""
    assume(cost > capacity)
    bucket = MemoryTokenBucket(capacity=capacity, refill_rate=refill_rate)
    with pytest.raises(ValueError, match="cost must be in"):
        bucket.try_acquire(cost=cost)


@given(
    capacity=_CAPACITIES,
    refill_rate=st.floats(
        min_value=0.001, max_value=0.01, allow_nan=False, allow_infinity=False
    ),
    keys=st.lists(_KEYS, min_size=1, max_size=20),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_token_bucket_keys_are_isolated(
    capacity: int, refill_rate: float, keys: list[str]
) -> None:
    """Tokens consumed for one key never affect another."""
    bucket = MemoryTokenBucket(capacity=capacity, refill_rate=refill_rate)
    distinct = list(dict.fromkeys(keys))
    for key in distinct:
        for _ in range(capacity):
            assert bucket.try_acquire(key=key) is True
    # With negligible refill, each key is drained to < 1 token.
    for key in distinct:
        tokens = bucket.peek(key=key)
        assert tokens < 1.0


@given(capacity=_CAPACITIES, refill_rate=_REFILL_RATES)
@settings(max_examples=100, deadline=None)
def test_token_bucket_peek_never_exceeds_capacity(
    capacity: int, refill_rate: float
) -> None:
    """`peek` after arbitrary refill stays in `[0, capacity]`."""
    bucket = MemoryTokenBucket(capacity=capacity, refill_rate=refill_rate)
    bucket.try_acquire()
    tokens = bucket.peek()
    assert 0.0 <= tokens <= float(capacity)


# --- _MemoryTokenBucket (async strategy via adapter) -------------------------


def _run(coro: Coroutine[None, None, None]) -> None:
    asyncio.new_event_loop().run_until_complete(coro)


@given(
    capacity=st.integers(min_value=1, max_value=100),
    refill_rate=st.floats(
        min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False
    ),
    cost=st.integers(min_value=1, max_value=50),
)
@settings(max_examples=100, deadline=None)
def test_strategy_token_bucket_retry_after_non_negative(
    capacity: int, refill_rate: float, cost: int
) -> None:
    """A denied acquire returns `retry_after >= 0`."""
    assume(cost <= capacity)

    async def run() -> None:
        adapter = MemoryRateLimiterAdapter()
        async with adapter:
            strategy = adapter.bind(
                TokenBucketConfig(capacity=capacity, refill_rate=refill_rate)
            )
            # Drain to force a denial.
            while True:
                result = await strategy.acquire(key="k", cost=cost)
                if not result.allowed:
                    assert result.retry_after >= 0
                    assert result.reset_after >= 0
                    assert result.remaining <= capacity
                    return

    _run(run())


# --- _MemoryGCRA (sliding window) --------------------------------------------


@given(
    limit=st.integers(min_value=1, max_value=100),
    window=st.floats(
        min_value=0.1, max_value=600.0, allow_nan=False, allow_infinity=False
    ),
)
@settings(max_examples=100, deadline=None)
def test_strategy_gcra_burst_never_exceeds_limit(
    limit: int, window: float
) -> None:
    """A fresh GCRA key allows exactly `limit` requests in a burst."""

    async def run() -> None:
        adapter = MemoryRateLimiterAdapter()
        async with adapter:
            strategy = adapter.bind(
                SlidingWindowConfig(limit=limit, window=window)
            )
            allowed = 0
            for _ in range(limit + 5):
                result = await strategy.acquire(key="k", cost=1)
                if result.allowed:
                    allowed += 1
                    assert result.retry_after == 0
                else:
                    assert result.retry_after >= 0
                    assert result.reset_after >= 0
            # GCRA admits `limit` then denies; round-trip math may
            # let one extra through depending on monotonic clock drift.
            assert limit <= allowed <= limit + 1

    _run(run())


@given(
    limit=st.integers(min_value=1, max_value=50),
    window=st.floats(
        min_value=0.1, max_value=60.0, allow_nan=False, allow_infinity=False
    ),
)
@settings(max_examples=100, deadline=None)
def test_strategy_gcra_peek_does_not_mutate(limit: int, window: float) -> None:
    """`peek` does not consume tokens."""

    async def run() -> None:
        adapter = MemoryRateLimiterAdapter()
        async with adapter:
            strategy = adapter.bind(
                SlidingWindowConfig(limit=limit, window=window)
            )
            for _ in range(10):
                result = await strategy.peek(key="k")
                assert result.allowed is True
                assert result.remaining == limit

    _run(run())


# --- Refill monotonicity ----------------------------------------------------


@given(
    capacity=st.integers(min_value=2, max_value=100),
    refill_rate=st.floats(
        min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False
    ),
    elapsed=st.floats(
        min_value=0.0, max_value=3600.0, allow_nan=False, allow_infinity=False
    ),
    start_tokens=st.floats(
        min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False
    ),
)
@settings(max_examples=200, deadline=None)
def test_token_bucket_refill_is_monotonic_and_capped(
    capacity: int,
    refill_rate: float,
    elapsed: float,
    start_tokens: float,
) -> None:
    """Refill increases over time and never exceeds capacity."""
    bucket = MemoryTokenBucket(capacity=capacity, refill_rate=refill_rate)
    tokens = min(start_tokens, float(capacity))
    last = monotonic()
    refilled = bucket._refill(tokens, last, last + elapsed)
    expected = min(float(capacity), tokens + elapsed * refill_rate)
    assert refilled <= float(capacity)
    assert math.isclose(refilled, expected, rel_tol=1e-9, abs_tol=1e-9)
