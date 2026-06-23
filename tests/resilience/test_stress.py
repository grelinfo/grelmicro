"""Stress tests for the in-memory resilience primitives.

Opt-in only. Every test carries both ``stress`` and ``slow`` so the
default unit/coverage job (``-m "not integration and not slow and not
stress"``) skips them. Run them with ``-m stress``.

Op-counts are bounded so each test settles in well under a second.
These are fast in-memory churn tests, not real-time endurance runs.
"""

import asyncio

import pytest

from grelmicro import Grelmicro
from grelmicro.resilience import CircuitBreakerRegistry, RateLimiterRegistry
from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerState,
)
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter import RateLimiter, TokenBucketConfig
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

pytestmark = [pytest.mark.stress, pytest.mark.slow]

ACQUIRES = 5_000
CONCURRENT_TASKS = 50
ACQUIRES_PER_TASK = 100
CHURN_CYCLES = 1_000


async def test_rate_limiter_throughput() -> None:
    """Thousands of sequential acquires stay consistent and bounded."""
    backend = MemoryRateLimiterAdapter()
    async with Grelmicro(uses=[RateLimiterRegistry(backend)]):
        limiter = RateLimiter(
            "throughput",
            TokenBucketConfig(capacity=ACQUIRES, refill_rate=ACQUIRES),
        )

        allowed = 0
        for _ in range(ACQUIRES):
            result = await limiter.acquire(key="user:1")
            if result.allowed:
                allowed += 1

        # Capacity equals the op-count, so every acquire is admitted.
        assert allowed == ACQUIRES


async def test_rate_limiter_concurrent_tasks() -> None:
    """Many concurrent tasks share one limiter without losing accounting."""
    backend = MemoryRateLimiterAdapter()
    total = CONCURRENT_TASKS * ACQUIRES_PER_TASK
    async with Grelmicro(uses=[RateLimiterRegistry(backend)]):
        limiter = RateLimiter(
            "concurrent",
            TokenBucketConfig(capacity=total, refill_rate=total),
        )
        allowed = 0

        async def hammer() -> None:
            nonlocal allowed
            for _ in range(ACQUIRES_PER_TASK):
                result = await limiter.acquire(key="shared")
                if result.allowed:
                    allowed += 1

        async with asyncio.TaskGroup() as tg:
            for _ in range(CONCURRENT_TASKS):
                tg.create_task(hammer())

        # The bucket holds exactly `total` tokens, so the concurrent
        # tasks consume all of them and none over-counts.
        assert allowed == total


async def test_circuit_breaker_churn() -> None:
    """Repeatedly drive open -> half_open -> closed and back."""
    backend = MemoryCircuitBreakerAdapter()
    async with Grelmicro(uses=[CircuitBreakerRegistry(backend)]):
        cb = CircuitBreaker.consecutive_count(
            "churn",
            error_threshold=1,
            success_threshold=1,
            half_open_capacity=1,
        )

        for _ in range(CHURN_CYCLES):
            await cb.transition_to_open()
            assert cb.state is CircuitBreakerState.OPEN
            await cb.transition_to_half_open()
            assert cb.state is CircuitBreakerState.HALF_OPEN
            await cb.transition_to_closed()
            assert cb.state is CircuitBreakerState.CLOSED

        assert cb.state is CircuitBreakerState.CLOSED
