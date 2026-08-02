"""The memory adapters count per worker, and the docs say so.

A memory adapter holds its state in the interpreter that created it, so a
second worker starts from zero. That is the documented behaviour, not a
defect, and these tests pin it: a change that quietly made a memory
adapter shared, or that made a distributed adapter per worker, fails here
instead of surprising a reader who scaled to four workers.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from .harness import run_workers

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier

    from .harness import Results

pytestmark = [pytest.mark.timeout(120)]

WORKERS = 3
LIMIT = 4


def spend_the_budget(barrier: Barrier, results: Results) -> None:
    """Spend one worker's rate-limit budget and report what it got."""
    asyncio.run(_spend_the_budget(barrier, results))


async def _spend_the_budget(barrier: Barrier, results: Results) -> None:
    from grelmicro.resilience import RateLimiter  # noqa: PLC0415
    from grelmicro.resilience.ratelimiter.memory import (  # noqa: PLC0415
        MemoryRateLimiterAdapter,
    )

    backend = MemoryRateLimiterAdapter()
    async with backend:
        limiter = RateLimiter.sliding_window(
            "per-worker", limit=LIMIT, window=60.0, backend=backend
        )
        barrier.wait()
        allowed = 0
        for _ in range(LIMIT * 2):
            outcome = await limiter.acquire(key="shared-caller")
            allowed += outcome.allowed
        results.append(allowed)


def test_memory_rate_limiter_gives_each_worker_its_own_budget() -> None:
    """Each worker admits the full limit, so N workers admit N times it."""
    # Act
    allowed = run_workers(spend_the_budget, WORKERS)

    # Assert
    assert allowed == [LIMIT] * WORKERS
    assert sum(allowed) == LIMIT * WORKERS
