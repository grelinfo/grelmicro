"""Exactly one process may believe it leads.

Leader election reads leadership back as `record.holder == <this worker>`,
so its token is the worker identity itself rather than a per-task token.
A pre-fork server hands every child the identity generated in the parent,
which would make each child match the holder and lead at the same time.
This drives the fork path on purpose, because it is the one that breaks.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from grelmicro.coordination._tokens import generate_worker_id

from .harness import run_workers

if TYPE_CHECKING:
    from collections.abc import Iterator
    from multiprocessing.synchronize import Barrier

    from .harness import Results

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(180),
    pytest.mark.skipif(sys.platform == "win32", reason="fork is POSIX only"),
    # `os.fork` in a process that already has threads is deprecated, and the
    # suite turns warnings into errors. Forking is the deployment under test.
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

pytest.importorskip("testcontainers.redis")

from testcontainers.redis import RedisContainer  # noqa: E402

WORKERS = 4
ELECTION_NAME = "multiprocess-election"
LEASE_DURATION = 5.0
SETTLE_SECONDS = 1.5

PRELOADED_WORKER = generate_worker_id()
"""The identity a preloaded app generates once, before it forks.

Every child inherits this exact string, which is the whole hazard. Passing
it explicitly reproduces `gunicorn --preload` without having to build the
entire application in the parent.
"""


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    """Yield a host-reachable URL for a module-scoped Redis container."""
    with RedisContainer() as container:
        yield f"redis://localhost:{container.get_exposed_port(6379)}/0"


def stand_for_election(barrier: Barrier, results: Results, url: str) -> None:
    """Run for leader and report whether this process won."""
    asyncio.run(_stand_for_election(barrier, results, url))


async def _stand_for_election(
    barrier: Barrier, results: Results, url: str
) -> None:
    from grelmicro.coordination import LeaderElection  # noqa: PLC0415
    from grelmicro.coordination.redis import (  # noqa: PLC0415
        RedisLeaderElectionAdapter,
    )

    os.environ["REDIS_URL"] = url
    election = LeaderElection(
        ELECTION_NAME,
        backend=RedisLeaderElectionAdapter(),
        worker=PRELOADED_WORKER,
        lease_duration=LEASE_DURATION,
        renew_deadline=LEASE_DURATION * 0.66,
        retry_interval=LEASE_DURATION * 0.2,
        backend_timeout=LEASE_DURATION * 0.5,
    )
    async with election.backend:
        barrier.wait()
        async with asyncio.TaskGroup() as group:
            task = group.create_task(election())
            # Every worker has raced by now, so whoever holds the lease
            # holds it. Read leadership once, then stand down.
            await asyncio.sleep(SETTLE_SECONDS)
            results.append((os.getpid(), election.is_leader(), time.time()))
            task.cancel()


def test_exactly_one_forked_worker_leads(redis_url: str) -> None:
    """Four children of a preloaded app elect one leader between them."""
    # Act
    outcomes = run_workers(
        stand_for_election, WORKERS, redis_url, start_method="fork"
    )

    # Assert
    assert len(outcomes) == WORKERS
    assert len({pid for pid, _, _ in outcomes}) == WORKERS
    leaders = [pid for pid, is_leader, _ in outcomes if is_leader]
    assert len(leaders) == 1
