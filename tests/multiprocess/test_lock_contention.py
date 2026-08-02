"""A distributed lock must exclude across processes, not only across tasks.

The rest of the suite proves exclusion between asyncio tasks in one
interpreter. A deployment runs several worker processes, so the guarantee
that matters is the one the backend enforces between them. Each worker
records when it entered and left the lock, and the parent reconstructs the
intervals and fails on any overlap.

Intervals are stamped with `time.time()` rather than `time.monotonic()`,
because only the wall clock is defined to share a reference point across
processes.
"""

from __future__ import annotations

import asyncio
import os
import time
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from .harness import run_workers

if TYPE_CHECKING:
    from collections.abc import Iterator
    from multiprocessing.synchronize import Barrier

    from .harness import Results

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(180),
]

pytest.importorskip("testcontainers.redis")

from testcontainers.redis import RedisContainer  # noqa: E402

WORKERS = 4
LOCK_NAME = "multiprocess-contention"
HOLD_SECONDS = 0.05


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    """Yield a host-reachable URL for a module-scoped Redis container."""
    with RedisContainer() as container:
        yield f"redis://localhost:{container.get_exposed_port(6379)}/0"


def hold_the_lock(barrier: Barrier, results: Results, url: str) -> None:
    """Acquire the lock once and report the interval it was held."""
    asyncio.run(_hold_the_lock(barrier, results, url))


async def _hold_the_lock(barrier: Barrier, results: Results, url: str) -> None:
    from grelmicro import Grelmicro  # noqa: PLC0415
    from grelmicro.coordination import Coordination  # noqa: PLC0415
    from grelmicro.coordination.redis import RedisLockAdapter  # noqa: PLC0415

    os.environ["REDIS_URL"] = url
    micro = Grelmicro(uses=[Coordination(lock=RedisLockAdapter())])
    async with micro:
        barrier.wait()
        async with micro.coordination.lock(LOCK_NAME):
            entered = time.time()
            await asyncio.sleep(HOLD_SECONDS)
            results.append((os.getpid(), entered, time.time()))


def test_only_one_process_holds_the_lock_at_a_time(redis_url: str) -> None:
    """No two workers hold the same lock over overlapping intervals."""
    # Act
    held = run_workers(hold_the_lock, WORKERS, redis_url)

    # Assert
    assert len(held) == WORKERS
    assert len({pid for pid, _, _ in held}) == WORKERS
    intervals = sorted((start, end) for _, start, end in held)
    overlaps = [
        (earlier, later)
        for earlier, later in pairwise(intervals)
        if later[0] < earlier[1]
    ]
    assert not overlaps
