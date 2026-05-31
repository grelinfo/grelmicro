"""Benchmark the in-memory sync lock request path.

Covers an acquire + release cycle against the in-memory sync
backend, which is the fast path used in tests and single-process
deployments.

Run with: python benchmarks/lock_benchmark.py
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from grelmicro.sync.lock import Lock
from grelmicro.sync.memory import MemorySyncAdapter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


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
    print(f"  {label:<28} {ns_per_op:>8.1f} ns/op")  # noqa: T201
    return ns_per_op


async def _bench_lock(iterations: int) -> None:
    """Measure an acquire + release cycle on the in-memory lock."""
    async with MemorySyncAdapter() as backend:
        lock = Lock("bench", backend=backend)

        async def acquire_release() -> None:
            await lock.acquire()
            await lock.release()

        await _measure_async(
            "acquire + release",
            acquire_release,
            iterations,
        )


def main() -> None:
    """Run all benchmarks."""
    print("=" * 60)  # noqa: T201
    print("In-memory sync lock benchmark")  # noqa: T201
    print("=" * 60)  # noqa: T201

    iterations = 500_000

    print(f"\nLock cycle, {iterations:,} iterations:\n")  # noqa: T201
    asyncio.run(_bench_lock(iterations))


if __name__ == "__main__":
    main()
