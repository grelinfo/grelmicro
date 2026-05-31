"""Benchmark the in-memory circuit breaker request path.

Covers the steady-state CLOSED path: a `try_acquire` admission
followed by `record_outcome(success=True)`.

Run with: python benchmarks/circuitbreaker_benchmark.py
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from grelmicro.resilience.circuitbreaker.consecutive_count import (
    ConsecutiveCountConfig,
)
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from grelmicro.resilience._protocol import CircuitBreakerStrategy


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


async def _bench_closed_path(iterations: int) -> None:
    """Measure admission and success recording on a CLOSED breaker."""
    async with MemoryCircuitBreakerAdapter() as adapter:
        strategy: CircuitBreakerStrategy = adapter.bind(
            name="bench",
            config=ConsecutiveCountConfig(),
        )

        await _measure_async(
            "try_acquire (CLOSED)",
            strategy.try_acquire,
            iterations,
        )
        await _measure_async(
            "record_outcome (success)",
            lambda: strategy.record_outcome(success=True),
            iterations,
        )

        async def call() -> None:
            await strategy.try_acquire()
            await strategy.record_outcome(success=True)

        await _measure_async(
            "try_acquire + record (success)",
            call,
            iterations,
        )


def main() -> None:
    """Run all benchmarks."""
    print("=" * 60)  # noqa: T201
    print("In-memory circuit breaker benchmark")  # noqa: T201
    print("=" * 60)  # noqa: T201

    iterations = 1_000_000

    print(f"\nClosed-breaker path, {iterations:,} iterations:\n")  # noqa: T201
    asyncio.run(_bench_closed_path(iterations))


if __name__ == "__main__":
    main()
