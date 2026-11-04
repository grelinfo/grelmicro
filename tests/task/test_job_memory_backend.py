"""Test what only the Memory job backend promises."""

from datetime import timedelta

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
