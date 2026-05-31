"""Benchmark the in-memory `TTLCache` request path.

Covers `get` hit, `get` miss, and `set` against the in-memory
cache backend with bytes values (no serializer overhead).

Run with: python benchmarks/cache_benchmark.py
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from grelmicro.cache import TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter

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
    print(f"  {label:<24} {ns_per_op:>8.1f} ns/op")  # noqa: T201
    return ns_per_op


async def _bench_cache(iterations: int) -> None:
    """Measure get hit, get miss, and set on the in-memory cache."""
    async with MemoryCacheAdapter() as backend:
        # No serializer: raw bytes round-trip straight through.
        cache: TTLCache[bytes] = TTLCache(ttl=3600, backend=backend)
        await cache.set("hit", b"value")

        await _measure_async(
            "get (hit)",
            lambda: cache.get("hit"),
            iterations,
        )
        await _measure_async(
            "get (miss)",
            lambda: cache.get("missing"),
            iterations,
        )
        await _measure_async(
            "set",
            lambda: cache.set("hit", b"value"),
            iterations,
        )


def main() -> None:
    """Run all benchmarks."""
    print("=" * 60)  # noqa: T201
    print("In-memory TTLCache benchmark")  # noqa: T201
    print("=" * 60)  # noqa: T201

    iterations = 1_000_000

    print(f"\nCache operations, {iterations:,} iterations:\n")  # noqa: T201
    asyncio.run(_bench_cache(iterations))


if __name__ == "__main__":
    main()
