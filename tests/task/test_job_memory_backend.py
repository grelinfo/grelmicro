"""Test what only the Memory job backend promises."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest

from grelmicro.clock import VirtualClock
from grelmicro.task import JobStatus
from grelmicro.task.memory import MemoryJobAdapter

NAME = "app:job"


async def test_job_memory_backend_purge_counts_up_to_its_limit() -> None:
    """Memory has no key expiry, so `purge` forgets and counts the jobs."""
    # Arrange
    clock = VirtualClock()
    backend = MemoryJobAdapter()
    async with clock, backend:
        for job_id in ("a", "b"):
            await backend.submit(
                name=NAME,
                job_id=job_id,
                owner=None,
                payload="{}",
                context={},
                max_queued=None,
                max_wait=None,
                keep=timedelta(seconds=60),
                start_as="r",
                lease=timedelta(seconds=30),
            )
            await backend.settle(
                name=NAME,
                job_id=job_id,
                runner="r",
                status=JobStatus.SUCCEEDED,
                error=None,
                result=None,
                progress=None,
            )
        await clock.advance(60)

        # Act
        first = await backend.purge(limit=1)
        second = await backend.purge(limit=10)
        third = await backend.purge(limit=10)

    # Assert
    assert (first, second, third) == (1, 1, 0)


async def _settled(backend: MemoryJobAdapter, _clock: VirtualClock) -> None:
    """End the job through `settle`."""
    await backend.settle(
        name=NAME,
        job_id="j",
        runner="r",
        status=JobStatus.SUCCEEDED,
        error=None,
        result=None,
        progress=None,
    )


async def _lease_lapsed(backend: MemoryJobAdapter, clock: VirtualClock) -> None:
    """End the job through a lapsed lease."""
    await clock.advance(31)
    await backend.reap(limit=10)


@pytest.mark.parametrize(
    "end",
    [
        pytest.param(_settled, id="settle"),
        pytest.param(_lease_lapsed, id="lost"),
    ],
)
async def test_job_memory_backend_forgets_arguments_of_a_running_job_that_ends(
    end: Callable[[MemoryJobAdapter, VirtualClock], Awaitable[Any]],
) -> None:
    """A finished job holds its arguments no longer, while `keep` runs."""
    # Arrange
    clock = VirtualClock()
    backend = MemoryJobAdapter()
    async with clock, backend:
        await backend.submit(
            name=NAME,
            job_id="j",
            owner=None,
            payload='{"secret": 1}',
            context={"traceparent": "t"},
            max_queued=None,
            max_wait=None,
            keep=timedelta(seconds=600),
            start_as="r",
            lease=timedelta(seconds=30),
        )

        # Act
        await end(backend, clock)

        # Assert
        row = backend._rows["j"]
        assert (row.payload, row.context) == ("", {})


@pytest.mark.parametrize("how", ["cancel", "past-max-wait"])
async def test_job_memory_backend_forgets_arguments_of_a_queued_job_that_ends(
    how: str,
) -> None:
    """A job that never ran holds its arguments no longer either."""
    # Arrange
    clock = VirtualClock()
    backend = MemoryJobAdapter()
    async with clock, backend:
        await backend.submit(
            name=NAME,
            job_id="j",
            owner=None,
            payload='{"secret": 1}',
            context={"traceparent": "t"},
            max_queued=None,
            max_wait=timedelta(seconds=10),
            keep=timedelta(seconds=600),
            start_as=None,
            lease=timedelta(seconds=30),
        )

        # Act
        if how == "cancel":
            await backend.cancel(name=NAME, job_id="j", owner=None)
        else:
            await clock.advance(11)
            await backend.reap(limit=10)

        # Assert
        row = backend._rows["j"]
        assert (row.payload, row.context) == ("", {})
