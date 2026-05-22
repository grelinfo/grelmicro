"""Retry budget bucket.

Holds a consecutive-failure counter shared across all retries on one
`Shield` instance. Each retry costs one unit. Refunds clamp at the
configured capacity.
"""

from __future__ import annotations

import asyncio

__all__ = ["_RetryBudget"]


class _RetryBudget:
    """Consecutive-failure counter gating retries on a `Shield` instance.

    Holds an integer count of available retries (the budget) bounded by
    `capacity`. Each retry takes one token via `try_acquire`. Refunds
    return tokens to the bucket, clamped at capacity.

    Refund rules evaluated once per call resolution:

    - Success with no retry performed: refund 1.
    - Success after one or more retries: refund 1 per recovered retry.
    - Failure surfaced after retries: no refund.

    Process-local. Guarded by an `asyncio.Lock` for safe sharing across
    coroutines on the same event loop.
    """

    __slots__ = ("_available", "_capacity", "_lock")

    def __init__(self, capacity: int) -> None:
        """Initialize a full bucket with the given capacity."""
        if capacity <= 0:
            msg = f"capacity must be positive, got {capacity!r}"
            raise ValueError(msg)
        self._capacity = capacity
        self._available = capacity
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        """Return the maximum number of tokens the bucket can hold."""
        return self._capacity

    @property
    def available(self) -> int:
        """Return the current number of available tokens."""
        return self._available

    async def try_acquire(self) -> bool:
        """Acquire one token. Return True on success, False when empty.

        Does not raise on an empty bucket. The caller stops retrying
        and surfaces the underlying failure.
        """
        async with self._lock:
            if self._available <= 0:
                return False
            self._available -= 1
            return True

    async def refund(self, amount: int) -> None:
        """Return `amount` tokens, clamped at the bucket capacity."""
        if amount <= 0:
            return
        async with self._lock:
            self._available = min(self._capacity, self._available + amount)
