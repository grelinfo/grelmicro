"""Test jobs across two replicas that share one store."""

import asyncio
from typing import Any

import pytest

from grelmicro.clock import VirtualClock
from grelmicro.task import Job, JobStatus, Progress, Tasks
from grelmicro.task.memory import MemoryJobAdapter
from tests.task._job_shared import share, shared

pytestmark = [pytest.mark.timeout(10)]

_PAST_THE_LEASE = 31
"""Seconds past the 30 second lease."""

_DONE = 3
"""Items a body reports done."""


class _Replica(MemoryJobAdapter):
    """One replica's client to a store every replica shares."""

    def __init__(self, store: MemoryJobAdapter) -> None:
        vars(self).update(vars(store))
        self.down = False
        """Whether this replica can no longer reach the store."""

    def _reach(self) -> None:
        if self.down:
            msg = "store unreachable"
            raise ConnectionError(msg)

    async def submit(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self._reach()
        return await super().submit(**kwargs)

    async def claim(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self._reach()
        return await super().claim(**kwargs)

    async def beat(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self._reach()
        return await super().beat(**kwargs)

    async def settle(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self._reach()
        return await super().settle(**kwargs)

    async def get(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self._reach()
        return await super().get(**kwargs)

    async def cancel(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self._reach()
        return await super().cancel(**kwargs)

    async def reap(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self._reach()
        return await super().reap(**kwargs)

    async def purge(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self._reach()
        return await super().purge(**kwargs)


async def _works(
    progress: Progress, gate: str, seen: str, replica: str
) -> None:
    """Note `replica` in `seen`, report items, and wait for `gate`.

    A cancelled wait is noted in `seen` too.
    """
    shared(seen).append(replica)
    progress.advance(_DONE)
    try:
        await shared(gate).wait()
    except asyncio.CancelledError:
        shared(seen).append(f"{replica} cancelled")
        raise


def _replica(
    store: MemoryJobAdapter,
    name: str = "import",
    **options: Any,  # noqa: ANN401
) -> tuple[Tasks, Job[...], _Replica]:
    """Return a replica's tasks and job, on its own client to `store`."""
    tasks = Tasks()
    client = _Replica(store)
    job = tasks.job(name=name, backend=client, **options)(_works)
    return tasks, job, client


async def _settled() -> None:
    """Let every ready task run."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _advance(clock: VirtualClock, seconds: int) -> None:
    """Move time forward second by second, so each beat runs."""
    for _ in range(seconds):
        await clock.advance(1)
        await _settled()


async def test_job_submitted_on_one_replica_is_cancelled_from_another() -> None:
    """Another replica reads the job, and its cancel stops the body."""
    # Arrange
    clock = VirtualClock()
    store = MemoryJobAdapter()
    one, job_one, _ = _replica(store)
    other, job_other, _ = _replica(store)
    seen: list[str] = []

    async with clock, one, other:
        submitted = await job_one.without_owner().submit(
            share(asyncio.Event()), share(seen), "one"
        )
        await _settled()
        read = await job_other.without_owner().get(submitted.id)

        # Act
        await job_other.without_owner().cancel(submitted.id)
        await _advance(clock, 1)
        ended = await job_other.without_owner().get(submitted.id)

    # Assert
    assert read is not None
    assert read.status is JobStatus.RUNNING
    assert seen == ["one", "one cancelled"]
    assert ended is not None
    assert ended.status is JobStatus.CANCELLED


async def test_job_progress_reaches_another_replica_with_the_next_beat() -> (
    None
):
    """Counts reported on one replica read on another one beat later."""
    # Arrange
    clock = VirtualClock()
    store = MemoryJobAdapter()
    one, job_one, _ = _replica(store)
    other, job_other, _ = _replica(store)
    gate = asyncio.Event()

    async with clock, one, other:
        submitted = await job_one.without_owner().submit(
            share(gate), share([]), "one"
        )
        await _settled()
        before = await job_other.without_owner().get(submitted.id)

        # Act
        await _advance(clock, 1)
        after = await job_other.without_owner().get(submitted.id)
        gate.set()

    # Assert
    assert before is not None
    assert before.done == 0
    assert after is not None
    assert after.done == _DONE


async def test_job_queued_on_a_full_replica_runs_on_a_free_one() -> None:
    """A job one replica has no slot for runs on another within a beat."""
    # Arrange
    clock = VirtualClock()
    store = MemoryJobAdapter()
    one, job_one, _ = _replica(store, max_concurrent=1)
    other, _, _ = _replica(store)
    busy, free = asyncio.Event(), asyncio.Event()
    free.set()

    async with clock, one:
        await job_one.without_owner().submit(share(busy), share([]), "one")
        await _settled()
        queued = await job_one.without_owner().submit(
            share(free), share([]), "one"
        )

        # Act
        async with other:
            await _advance(clock, 1)
            ran = await job_one.without_owner().get(queued.id)
        busy.set()

    # Assert
    assert queued.status is JobStatus.QUEUED
    assert ran is not None
    assert ran.status is JobStatus.SUCCEEDED


async def test_job_replica_claims_only_the_names_it_runs() -> None:
    """A free replica leaves alone a queued job of a name it does not run."""
    # Arrange
    clock = VirtualClock()
    store = MemoryJobAdapter()
    importer, _, _ = _replica(store, "import")
    exporter, export, _ = _replica(store, "export", max_concurrent=1)
    gate = asyncio.Event()
    seen: list[str] = []

    async with clock, importer, exporter:
        await export.without_owner().submit(share(gate), share(seen), "busy")
        await _settled()
        queued = await export.without_owner().submit(
            share(gate), share(seen), "queued"
        )

        # Act
        await _advance(clock, 2)
        waiting = await export.without_owner().get(queued.id)
        gate.set()
        await _advance(clock, 1)

    # Assert
    assert waiting is not None
    assert waiting.status is JobStatus.QUEUED
    assert seen == ["busy", "queued"]


async def test_job_of_a_replica_that_lost_the_store_reads_lost_elsewhere() -> (
    None
):
    """A replica cut off from the store stops its body, and others read lost."""
    # Arrange
    clock = VirtualClock()
    store = MemoryJobAdapter()
    one, job_one, client_one = _replica(store)
    other, job_other, _ = _replica(store)
    seen: list[str] = []

    async with clock, one, other:
        submitted = await job_one.without_owner().submit(
            share(asyncio.Event()), share(seen), "one"
        )
        await _settled()

        # Act
        client_one.down = True
        await _advance(clock, _PAST_THE_LEASE)
        ended = await job_other.without_owner().get(submitted.id)
        stopped = seen.copy()
        client_one.down = False
        await _advance(clock, 2)

    # Assert
    assert stopped == ["one", "one cancelled"]
    assert ended is not None
    assert (ended.status, ended.error) == (JobStatus.FAILED, "Job lost")
