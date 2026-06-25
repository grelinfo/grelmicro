"""Tests for `RateLimiter.wait`, the blocking admission verb.

The wait loop sleeps on the clock seam, so a `VirtualClock` drives the
refill without real waiting.
"""

import asyncio
from collections.abc import AsyncGenerator

import pytest

from grelmicro import Grelmicro
from grelmicro.clock import VirtualClock
from grelmicro.resilience import RateLimiterRegistry
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.ratelimiter import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

pytestmark = [pytest.mark.timeout(1)]

CAPACITY = 1
REFILL_RATE = 1.0


@pytest.fixture
async def _backend() -> AsyncGenerator[MemoryRateLimiterAdapter]:
    """Open the in-memory backend inside an active `Grelmicro` app."""
    backend = MemoryRateLimiterAdapter()
    async with Grelmicro(uses=[RateLimiterRegistry(backend)]):
        yield backend


@pytest.fixture
def limiter(_backend: MemoryRateLimiterAdapter) -> RateLimiter:
    """Return a token-bucket limiter with one token, refilling at 1 tps."""
    return RateLimiter.token_bucket(
        "wait-tb", capacity=CAPACITY, refill_rate=REFILL_RATE
    )


async def test_wait_returns_immediately_when_allowed(
    limiter: RateLimiter,
) -> None:
    """A request within budget is admitted on the first attempt."""
    result = await limiter.wait()
    assert result.allowed


async def test_wait_blocks_until_refill(
    clock: VirtualClock, limiter: RateLimiter
) -> None:
    """A drained limiter blocks on the clock seam until a token refills."""
    assert await limiter.allow()  # drain the single token

    task = asyncio.create_task(limiter.wait())
    await asyncio.sleep(0)
    assert not task.done()  # suspended on the refill sleep

    await clock.advance(1.0)
    result = await task
    assert result.allowed


async def test_wait_succeeds_within_max_wait_budget(
    clock: VirtualClock, limiter: RateLimiter
) -> None:
    """A `max_wait` larger than the refill delay waits, then succeeds."""
    assert await limiter.allow()  # drain the single token

    task = asyncio.create_task(limiter.wait(max_wait=5.0))
    await asyncio.sleep(0)
    assert not task.done()  # suspended within the budget

    await clock.advance(1.0)
    result = await task
    assert result.allowed


async def test_wait_raises_when_cost_exceeds_capacity(
    limiter: RateLimiter,
) -> None:
    """An unsatisfiable cost raises instead of waiting forever."""
    with pytest.raises(ValueError, match="cost must be between"):
        await limiter.wait(cost=CAPACITY + 1)


async def test_wait_raises_when_max_wait_budget_exceeded(
    limiter: RateLimiter,
) -> None:
    """`max_wait` shorter than the refill delay raises immediately."""
    assert await limiter.allow()  # drain the single token

    with pytest.raises(RateLimitExceededError):
        await limiter.wait(max_wait=0.5)  # refill needs ~1s


async def test_wait_max_wait_zero_is_single_attempt(
    limiter: RateLimiter,
) -> None:
    """`max_wait=0` makes a single attempt and raises when denied."""
    assert await limiter.allow()  # drain the single token

    with pytest.raises(RateLimitExceededError):
        await limiter.wait(max_wait=0.0)
