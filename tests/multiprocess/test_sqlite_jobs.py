"""Processes sharing one SQLite file share its on-demand jobs.

Each worker opens its own `SQLiteProvider` on the same file, as each
worker of a server on one host does. A job submitted in one process is
claimed, beaten and settled in another, and racing processes never claim
the same job.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.task import JobStatus
from grelmicro.task._protocol import Beat, ProgressUpdate

from .harness import run_workers

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier
    from pathlib import Path

    from grelmicro.task._protocol import JobRecord

    from .harness import Results

pytestmark = [pytest.mark.timeout(120)]

NAME = "app:import_rows"
OWNER = "alice"
LEASE = timedelta(seconds=60)
"""A lease that outlives the run, so no job lapses while it is held."""

KEEP = timedelta(hours=1)
WORKERS = 2
JOBS = 20
BARRIER_TIMEOUT = 20.0
"""Seconds a worker waits for the others, so a crashed one fails the run."""


async def _submit(
    path: str, ids: list[str], *, max_queued: int | None = None
) -> int:
    """Submit queued jobs on the file, and return how many were stored."""
    stored = 0
    async with (
        SQLiteProvider(path) as provider,
        provider.job_backend() as backend,
    ):
        for job_id in ids:
            submitted = await backend.submit(
                name=NAME,
                job_id=job_id,
                owner=OWNER,
                payload='{"rows": 3}',
                context={"trace": "t"},
                max_queued=max_queued,
                max_wait=None,
                max_run=None,
                keep=KEEP,
                start_as=None,
                lease=LEASE,
            )
            stored += submitted.record is not None
    return stored


async def _read(path: str, job_id: str) -> JobRecord | None:
    """Read one job from the file."""
    async with (
        SQLiteProvider(path) as provider,
        provider.job_backend() as backend,
    ):
        return await backend.get(name=NAME, job_id=job_id, owner=OWNER)


def submit_one(barrier: Barrier, results: Results, path: str) -> None:
    """Submit one job, and report how many were stored."""
    barrier.wait(BARRIER_TIMEOUT)
    results.append(asyncio.run(_submit(path, ["handed-over"])))


def run_one(barrier: Barrier, results: Results, path: str) -> None:
    """Claim the job another process submitted, beat it, and settle it."""
    asyncio.run(_run_one(barrier, results, path))


async def _run_one(barrier: Barrier, results: Results, path: str) -> None:
    async with (
        SQLiteProvider(path) as provider,
        provider.job_backend() as backend,
    ):
        barrier.wait(BARRIER_TIMEOUT)
        [claimed] = await backend.claim(
            slots={NAME: 1}, runner="other-process", lease=LEASE
        )
        [reply] = await backend.beat(
            runner="other-process",
            beats=[Beat(NAME, claimed.record.id, ProgressUpdate(2, 3))],
            lease=LEASE,
        )
        settled = await backend.settle(
            name=NAME,
            job_id=claimed.record.id,
            runner="other-process",
            status=JobStatus.SUCCEEDED,
            error=None,
            result='"imported"',
            progress=ProgressUpdate(3, 3),
        )
    results.append(
        (claimed.payload, dict(claimed.context), reply.held, settled.settled)
    )


def test_job_submitted_in_one_process_runs_in_another(tmp_path: Path) -> None:
    """A second process claims, beats and settles the job the first stored."""
    # Arrange
    path = str(tmp_path / "jobs.db")
    assert run_workers(submit_one, 1, path) == [1]

    # Act
    [(payload, context, held, settled)] = run_workers(run_one, 1, path)
    read = asyncio.run(_read(path, "handed-over"))

    # Assert
    assert (payload, context) == ('{"rows": 3}', {"trace": "t"})
    assert held is True
    assert settled is True
    assert read is not None
    assert read.status is JobStatus.SUCCEEDED
    assert (read.done, read.total, read.result) == (3, 3, '"imported"')


def claim_until_empty(barrier: Barrier, results: Results, path: str) -> None:
    """Race the other workers for queued jobs, one at a time, until none is left."""
    asyncio.run(_claim_until_empty(barrier, results, path))


async def _claim_until_empty(
    barrier: Barrier, results: Results, path: str
) -> None:
    runner = f"runner-{os.getpid()}"
    taken: list[str] = []
    async with (
        SQLiteProvider(path) as provider,
        provider.job_backend() as backend,
    ):
        barrier.wait(BARRIER_TIMEOUT)
        while claimed := await backend.claim(
            slots={NAME: 1}, runner=runner, lease=LEASE
        ):
            for job in claimed:
                taken.append(job.record.id)
                await backend.settle(
                    name=NAME,
                    job_id=job.record.id,
                    runner=runner,
                    status=JobStatus.SUCCEEDED,
                    error=None,
                    result=None,
                    progress=None,
                )
    results.append(taken)


def test_processes_never_claim_the_same_job(tmp_path: Path) -> None:
    """Two processes racing for one queue take each job exactly once."""
    # Arrange
    path = str(tmp_path / "jobs.db")
    ids = [f"job-{index}" for index in range(JOBS)]
    assert asyncio.run(_submit(path, ids)) == JOBS

    # Act
    taken = run_workers(claim_until_empty, WORKERS, path)

    # Assert
    claimed = [job_id for worker in taken for job_id in worker]
    assert sorted(claimed) == sorted(ids)
    for job_id in ids:
        read = asyncio.run(_read(path, job_id))
        assert read is not None
        assert read.status is JobStatus.SUCCEEDED


MAX_QUEUED = 5


def submit_many(barrier: Barrier, results: Results, path: str) -> None:
    """Submit jobs against `MAX_QUEUED`, and report how many were stored."""
    ids = [f"{os.getpid()}-{index}" for index in range(JOBS)]
    barrier.wait(BARRIER_TIMEOUT)
    results.append(asyncio.run(_submit(path, ids, max_queued=MAX_QUEUED)))


def test_processes_never_queue_past_max_queued(tmp_path: Path) -> None:
    """Concurrent submits from two processes stop at `max_queued` together."""
    # Arrange
    path = str(tmp_path / "jobs.db")

    # Act
    stored = run_workers(submit_many, WORKERS, path)

    # Assert
    assert sum(stored) == MAX_QUEUED
