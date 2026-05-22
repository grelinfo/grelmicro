"""Retry budget bucket tests."""

from __future__ import annotations

import asyncio

import pytest

from grelmicro.resilience.shield._retry_budget import _RetryBudget


async def test_initial_state_full() -> None:
    """Bucket starts at capacity."""
    capacity = 10
    bucket = _RetryBudget(capacity=capacity)
    assert bucket.capacity == capacity
    assert bucket.available == capacity


async def test_acquire_consumes_one_token() -> None:
    """Each `try_acquire` removes one token."""
    bucket = _RetryBudget(capacity=3)
    assert await bucket.try_acquire() is True
    assert bucket.available == 2  # noqa: PLR2004


async def test_empty_bucket_denies_acquire() -> None:
    """An empty bucket returns False without raising."""
    bucket = _RetryBudget(capacity=1)
    assert await bucket.try_acquire() is True
    assert await bucket.try_acquire() is False
    assert bucket.available == 0


async def test_refund_clamps_at_capacity() -> None:
    """Refunds never exceed the capacity."""
    bucket = _RetryBudget(capacity=5)
    await bucket.try_acquire()
    await bucket.refund(100)
    assert bucket.available == 5  # noqa: PLR2004


async def test_zero_or_negative_refund_is_a_noop() -> None:
    """Refunding zero or a negative amount does nothing."""
    bucket = _RetryBudget(capacity=5)
    await bucket.try_acquire()
    await bucket.refund(0)
    await bucket.refund(-3)
    assert bucket.available == 4  # noqa: PLR2004


async def test_cost_n_then_refund_n_is_net_zero() -> None:
    """Acquiring N and refunding N leaves the bucket at capacity."""
    bucket = _RetryBudget(capacity=10)
    for _ in range(5):
        await bucket.try_acquire()
    await bucket.refund(5)
    assert bucket.available == 10  # noqa: PLR2004


def test_zero_capacity_is_rejected() -> None:
    """Capacity must be positive."""
    with pytest.raises(ValueError, match="capacity"):
        _RetryBudget(capacity=0)


async def test_lock_serialises_concurrent_acquires() -> None:
    """Concurrent `try_acquire` never double-counts."""
    bucket = _RetryBudget(capacity=5)

    async def grab() -> bool:
        return await bucket.try_acquire()

    results = await asyncio.gather(*(grab() for _ in range(10)))
    assert sum(results) == 5  # noqa: PLR2004
    assert bucket.available == 0
