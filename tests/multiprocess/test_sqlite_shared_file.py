"""Services sharing one SQLite file must not lock each other out.

Each worker opens its own `SQLiteProvider` on the same file and keeps it
open while the others open theirs and write. A provider that left a
statement unfinished on open would hold a lock on the file, and every
other worker would fail with `database is locked`.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from .harness import run_workers

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier
    from pathlib import Path

    from .harness import Results

pytestmark = [pytest.mark.timeout(120)]

WORKERS = 3
BARRIER_TIMEOUT = 20.0
"""Seconds a worker waits for the others, so a crashed one fails the run."""


def open_and_write(barrier: Barrier, results: Results, path: str) -> None:
    """Open a provider on the shared file, then write while all hold it."""
    asyncio.run(_open_and_write(barrier, results, path))


async def _open_and_write(
    barrier: Barrier, results: Results, path: str
) -> None:
    from grelmicro.providers.sqlite import SQLiteProvider  # noqa: PLC0415

    async with SQLiteProvider(path) as provider:
        barrier.wait(BARRIER_TIMEOUT)
        async with provider.cache_backend() as cache:
            key = str(os.getpid())
            await cache.set(key=key, value=b"v", ttl=timedelta(seconds=60))
            results.append(await cache.get(key=key))
        barrier.wait(BARRIER_TIMEOUT)


def test_workers_sharing_one_sqlite_file_all_open_and_write(
    tmp_path: Path,
) -> None:
    """Every worker opens the shared file and writes while the others hold it."""
    # Act
    written = run_workers(open_and_write, WORKERS, str(tmp_path / "shared.db"))

    # Assert
    assert written == [b"v"] * WORKERS


ELECTION_LEASE = timedelta(seconds=5)
"""A lease that outlives the run, so the first leader keeps it."""

SETTLE_SECONDS = 1.0
"""Seconds every worker stands before reading whether it leads."""


def stand_for_election(barrier: Barrier, results: Results, path: str) -> None:
    """Run for leader on the shared file and report whether this worker won."""
    asyncio.run(_stand_for_election(barrier, results, path))


async def _stand_for_election(
    barrier: Barrier, results: Results, path: str
) -> None:
    from grelmicro.coordination import LeaderElection  # noqa: PLC0415
    from grelmicro.providers.sqlite import SQLiteProvider  # noqa: PLC0415

    async with (
        SQLiteProvider(path) as provider,
        provider.leaderelection_backend() as backend,
    ):
        election = LeaderElection(
            "multiprocess-election",
            backend=backend,
            lease_duration=ELECTION_LEASE,
            renew_deadline=ELECTION_LEASE * 0.66,
            retry_interval=0.1,
            backend_timeout=2.0,
        )
        barrier.wait(BARRIER_TIMEOUT)
        async with asyncio.TaskGroup() as group:
            task = group.create_task(election())
            await asyncio.sleep(SETTLE_SECONDS)
            results.append(election.is_leader())
            # Every worker has read before the leader stands down and
            # releases, so no standby can take over before reading.
            barrier.wait(BARRIER_TIMEOUT)
            task.cancel()


def test_workers_sharing_one_sqlite_file_elect_one_leader(
    tmp_path: Path,
) -> None:
    """Worker processes on one SQLite file elect exactly one leader."""
    # Act
    leading = run_workers(
        stand_for_election, WORKERS, str(tmp_path / "election.db")
    )

    # Assert
    assert len(leading) == WORKERS
    assert sum(leading) == 1
