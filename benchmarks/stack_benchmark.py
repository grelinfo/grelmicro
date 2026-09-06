"""Benchmark the two ways a `Stack` reaches a call.

Covers the decorator, which wraps once, and `Stack.run`, which is
handed its target per call. Both run against in-memory backends, so
what is measured is the stack's own overhead.

Run with: python benchmarks/stack_benchmark.py
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    Pattern,
    RateLimiter,
    Retry,
    Stack,
    Timeout,
)
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

UNLIMITED = 10**9
"""Tokens and permits enough that nothing is ever refused."""


async def _measure(
    label: str, fn: Callable[[], Awaitable[object]], iterations: int
) -> float:
    """Return µs/op for an async `fn` over `iterations`."""
    for _ in range(1000):
        await fn()
    start = time.perf_counter()
    for _ in range(iterations):
        await fn()
    elapsed = time.perf_counter() - start
    us_per_op = elapsed / iterations * 1e6
    print(f"  {label:<34} {us_per_op:>8.2f} µs/op")  # noqa: T201
    return us_per_op


async def work(value: int) -> int:
    """Return the value, standing in for the call every stack wraps."""
    return value


async def _bench_stack(
    label: str, patterns: list[Pattern], iterations: int
) -> None:
    """Measure both forms of one stack."""
    stack = Stack("bench", patterns=patterns)
    decorated = stack(work)

    print(f"\n{label}, {iterations:,} iterations:\n")  # noqa: T201
    await _measure("decorated", lambda: decorated(1), iterations)
    await _measure("run", lambda: stack.run(work, 1), iterations)


async def _bench_all(iterations: int) -> None:
    """Measure a two, three, and six pattern stack."""
    async with (
        MemoryCircuitBreakerAdapter() as breaker_backend,
        MemoryRateLimiterAdapter() as limiter_backend,
    ):
        retry = Retry("bench", when=OSError, attempts=3)
        timeout = Timeout("bench", seconds=60)
        fallback = Fallback("bench", when=OSError, default=0)
        breaker = CircuitBreaker("bench", backend=breaker_backend)
        bulkhead = Bulkhead("bench", max_concurrent=UNLIMITED)
        limiter = RateLimiter.token_bucket(
            "bench",
            capacity=UNLIMITED,
            refill_rate=UNLIMITED,
            backend=limiter_backend,
        )

        await _bench_stack("2 patterns", [retry, timeout], iterations)
        await _bench_stack("3 patterns", [retry, timeout, fallback], iterations)
        await _bench_stack(
            "6 patterns",
            [fallback, retry, breaker, limiter, bulkhead, timeout],
            iterations,
        )


def main() -> None:
    """Run all benchmarks."""
    print("=" * 60)  # noqa: T201
    print("Resilience stack benchmark")  # noqa: T201
    print("=" * 60)  # noqa: T201

    asyncio.run(_bench_all(200_000))


if __name__ == "__main__":
    main()
