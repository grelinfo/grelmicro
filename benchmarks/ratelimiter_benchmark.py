"""Benchmark the in-memory rate limiter request path.

Covers the async `acquire` call for both algorithms (token bucket
and sliding-window GCRA) plus the synchronous
`MemoryTokenBucket.try_acquire` used by logging filters.

Run with: python benchmarks/ratelimiter_benchmark.py
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from grelmicro.resilience.ratelimiter.memory import (
    MemoryRateLimiterAdapter,
    MemoryTokenBucket,
)
from grelmicro.resilience.ratelimiter.sliding_window import SlidingWindowConfig
from grelmicro.resilience.ratelimiter.token_bucket import TokenBucketConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from grelmicro.resilience._protocol import RateLimiterStrategy


def _measure_sync(
    label: str, fn: Callable[[], object], iterations: int
) -> float:
    """Return ns/op for a sync `fn` over `iterations`."""
    for _ in range(1000):
        fn()
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    ns_per_op = elapsed / iterations * 1e9
    print(f"  {label:<34} {ns_per_op:>8.1f} ns/op")  # noqa: T201
    return ns_per_op


async def _measure_async(
    label: str, fn: Callable[[], Awaitable[object]], iterations: int
) -> float:
    """Return ns/op for an async `fn` over `iterations`."""
    for _ in range(1000):
        await fn()
    start = time.perf_counter()
    for _ in range(iterations):
        await fn()
    elapsed = time.perf_counter() - start
    ns_per_op = elapsed / iterations * 1e9
    print(f"  {label:<34} {ns_per_op:>8.1f} ns/op")  # noqa: T201
    return ns_per_op


async def _bench_acquire(iterations: int) -> None:
    """Measure the async acquire path for both algorithms."""
    async with MemoryRateLimiterAdapter() as adapter:
        token_bucket: RateLimiterStrategy = adapter.bind(
            # Huge capacity and refill so every acquire is allowed.
            TokenBucketConfig(capacity=1_000_000_000, refill_rate=1e9),
        )
        sliding_window: RateLimiterStrategy = adapter.bind(
            SlidingWindowConfig(limit=1_000_000_000, window=60),
        )

        await _measure_async(
            "TokenBucket.acquire (allowed)",
            lambda: token_bucket.acquire(key="bench", cost=1),
            iterations,
        )
        await _measure_async(
            "SlidingWindow.acquire (allowed)",
            lambda: sliding_window.acquire(key="bench", cost=1),
            iterations,
        )


def _bench_try_acquire(iterations: int) -> None:
    """Measure the synchronous token-bucket hit path."""
    bucket = MemoryTokenBucket(capacity=1_000_000_000, refill_rate=1e9)
    _measure_sync(
        "MemoryTokenBucket.try_acquire (hit)",
        lambda: bucket.try_acquire("bench", cost=1.0),
        iterations,
    )


def main() -> None:
    """Run all benchmarks."""
    print("=" * 60)  # noqa: T201
    print("In-memory rate limiter benchmark")  # noqa: T201
    print("=" * 60)  # noqa: T201

    iterations = 1_000_000

    print(f"\nAsync acquire, {iterations:,} iterations:\n")  # noqa: T201
    asyncio.run(_bench_acquire(iterations))

    print(f"\nSync try_acquire, {iterations:,} iterations:\n")  # noqa: T201
    _bench_try_acquire(iterations)


if __name__ == "__main__":
    main()
