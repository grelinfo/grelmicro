"""Test how on-demand jobs drain when their tasks shut down."""

import asyncio
import contextlib
import threading
from collections.abc import AsyncGenerator
from typing import Any, Self

import pytest

from grelmicro import Grelmicro
from grelmicro.task import Job, JobRefusedError, JobStatus, Progress, Tasks
from grelmicro.task._protocol import Settled
from grelmicro.task.memory import MemoryJobAdapter
from tests.task._job_shared import share, shared

pytestmark = [pytest.mark.timeout(10)]


async def _wait_for(_progress: Progress, gate: str) -> None:
    """Run until the shared `gate` is set."""
    await shared(gate).wait()


async def _settled(job: Job[...], job_id: str) -> JobStatus:
    """Return the status of `job_id`, which must still be known."""
    info = await job.without_owner().get(job_id)
    assert info is not None
    return info.status


@contextlib.asynccontextmanager
async def _draining(tasks: Tasks) -> AsyncGenerator[asyncio.Task[bool | None]]:
    """Leave `tasks` in the background and yield while it drains."""
    exiting = asyncio.create_task(tasks.__aexit__(None, None, None))
    for _ in range(5):
        await asyncio.sleep(0)
    yield exiting
    await exiting


async def test_job_submit_refused_during_the_drain() -> None:
    """Once shutdown starts, `submit` raises `JobRefusedError`."""
    # Arrange
    tasks = Tasks(shutdown_timeout=5)
    job = tasks.job()(_wait_for)
    gate = asyncio.Event()
    await tasks.__aenter__()
    await job.without_owner().submit(share(gate))

    async with _draining(tasks):
        # Act
        with pytest.raises(JobRefusedError) as refused:
            await job.without_owner().submit(share(gate))
        gate.set()

    # Assert
    assert refused.value.reason == "the tasks are shutting down"


async def test_job_submit_refused_after_the_drain() -> None:
    """Once the tasks have stopped, `submit` still raises `JobRefusedError`."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_wait_for)
    async with tasks:
        pass

    # Act
    with pytest.raises(JobRefusedError) as refused:
        await job.without_owner().submit(share(asyncio.Event()))

    # Assert
    assert refused.value.reason == "the tasks are shutting down"


async def _items_until_stopped(progress: Progress, gate: str) -> None:
    """Handle one item per `gate` set, stopping at the checkpoint."""
    event = shared(gate)
    while True:
        await event.wait()
        event.clear()
        progress.checkpoint()
        progress.advance()


async def test_job_drain_keeps_the_queue_and_stops_between_items() -> None:
    """Queued jobs stay queued, and a running one ends after its item."""
    # Arrange
    tasks = Tasks(shutdown_timeout=5)
    job = tasks.job(max_concurrent=1)(_items_until_stopped)
    gate = asyncio.Event()
    await tasks.__aenter__()
    running = await job.without_owner().submit(share(gate))
    queued = await job.without_owner().submit(share(gate))
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)

    async with _draining(tasks) as exiting:
        # Act
        queued_status = await _settled(job, queued.id)
        while_draining = await _settled(job, running.id)
        drained_early = exiting.done()
        gate.set()

    # Assert
    queued_info = await job.without_owner().get(queued.id)
    running_info = await job.without_owner().get(running.id)
    assert queued_status is JobStatus.QUEUED
    assert queued_info is not None
    assert queued_info.status is JobStatus.QUEUED
    assert queued_info.started_at is None
    assert while_draining is JobStatus.RUNNING
    assert drained_early is False
    assert running_info is not None
    assert running_info.status is JobStatus.CANCELLED
    assert running_info.done == 1


async def test_job_drain_cancels_a_job_past_shutdown_timeout() -> None:
    """A job that ignores `stopping` is cancelled at `shutdown_timeout`."""
    # Arrange
    tasks = Tasks(shutdown_timeout=0.05)
    job = tasks.job()(_wait_for)
    await tasks.__aenter__()
    submitted = await job.without_owner().submit(share(asyncio.Event()))

    # Act
    await tasks.__aexit__(None, None, None)

    # Assert
    assert await _settled(job, submitted.id) is JobStatus.CANCELLED


async def test_job_drain_with_no_timeout_ends_what_runs_cleanly() -> None:
    """A force-cancel at once ends what runs, and leaves the queue queued."""
    # Arrange
    tasks = Tasks(shutdown_timeout=0)
    job = tasks.job(max_concurrent=1)(_wait_for)
    await tasks.__aenter__()
    running = await job.without_owner().submit(share(asyncio.Event()))
    queued = await job.without_owner().submit(share(asyncio.Event()))
    await asyncio.sleep(0)

    # Act
    await tasks.__aexit__(None, None, None)

    # Assert
    assert await _settled(job, running.id) is JobStatus.CANCELLED
    assert await _settled(job, queued.id) is JobStatus.QUEUED
    with pytest.raises(JobRefusedError):
        await job.without_owner().submit(share(asyncio.Event()))


def _write_when_released(
    progress: Progress,
    started: str,
    release: str,
    finished: str,
    wrote: str,
) -> None:
    """Block in a thread until `release`, then try to report."""
    shared(started).set()
    shared(release).wait()
    try:
        progress.total = 1
        shared(wrote).append("total")
    finally:
        try:
            progress.advance()
            shared(wrote).append("advance")
        finally:
            shared(finished).set()


async def test_job_ended_at_shutdown_timeout_takes_no_late_writes() -> None:
    """A sync job ended by the timeout keeps the state it ended with."""
    # Arrange
    tasks = Tasks(shutdown_timeout=0.05)
    job = tasks.job()(_write_when_released)
    started, release = threading.Event(), threading.Event()
    finished = threading.Event()
    wrote: list[str] = []
    await tasks.__aenter__()
    submitted = await job.without_owner().submit(
        share(started), share(release), share(finished), share(wrote)
    )
    await asyncio.to_thread(started.wait)
    await tasks.__aexit__(None, None, None)

    # Act
    release.set()
    await asyncio.to_thread(finished.wait)

    # Assert
    info = await job.without_owner().get(submitted.id)
    assert info is not None
    assert info.status is JobStatus.CANCELLED
    assert (info.done, info.total) == (0, None)
    assert wrote == []


class _Abort(BaseException):
    """An error that is not an `Exception`."""


async def _abort(_progress: Progress) -> None:
    """Raise an error that is not an `Exception`."""
    raise _Abort


async def test_job_body_raising_a_base_exception_ends_failed() -> None:
    """A body raising a `BaseException` ends `failed`, and it propagates."""
    # Arrange
    tasks = Tasks()
    job = tasks.job()(_abort)
    submitted: list[str] = []

    # Act
    with pytest.RaisesGroup(_Abort, flatten_subgroups=True):
        async with tasks:
            submitted.append((await job.without_owner().submit()).id)
            await asyncio.Event().wait()

    # Assert
    info = await job.without_owner().get(submitted[0])
    assert info is not None
    assert info.status is JobStatus.FAILED
    assert info.error == "Job failed"


async def test_job_base_exception_starts_no_queued_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a `BaseException`, this replica starts no queued job."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(max_concurrent=1)(_abort)
    submitted: list[str] = []

    # Act
    with pytest.RaisesGroup(_Abort, flatten_subgroups=True):
        async with tasks:
            submitted.append((await job.without_owner().submit()).id)
            submitted.append((await job.without_owner().submit()).id)
            await asyncio.Event().wait()

    # Assert
    assert await _settled(job, submitted[1]) is JobStatus.QUEUED
    assert "Exception in callback" not in caplog.text


def _sync_until_released(
    progress: Progress, started: str, release: str
) -> None:
    """Block in a thread until `release`, then stop at the checkpoint."""
    shared(started).set()
    shared(release).wait()
    progress.checkpoint()
    progress.advance()


async def test_job_drain_stops_a_sync_job_at_its_checkpoint() -> None:
    """A sync body sees `stopping` and ends at its next `checkpoint()`."""
    # Arrange
    tasks = Tasks(shutdown_timeout=5)
    job = tasks.job()(_sync_until_released)
    started, release = threading.Event(), threading.Event()
    await tasks.__aenter__()
    submitted = await job.without_owner().submit(share(started), share(release))
    await asyncio.to_thread(started.wait)

    async with _draining(tasks):
        # Act
        release.set()

    # Assert
    info = await job.without_owner().get(submitted.id)
    assert info is not None
    assert info.status is JobStatus.CANCELLED
    assert info.done == 0


class _Database:
    """A resource the app opens before its tasks and closes after them."""

    def __init__(self) -> None:
        self.open = False

    async def __aenter__(self) -> Self:
        self.open = True
        return self

    async def __aexit__(self, *_: object) -> None:
        self.open = False


async def _use_database_on_stop(
    progress: Progress, database: str, seen: str
) -> None:
    """Wait for shutdown, then record whether the database is open."""
    while not progress.stopping:  # noqa: ASYNC110
        await asyncio.sleep(0)
    shared(seen).append(shared(database).open)


async def test_job_app_drains_jobs_before_closing_its_resources() -> None:
    """A draining job still reaches what the app opened before its tasks."""
    # Arrange
    database = _Database()
    tasks = Tasks(shutdown_timeout=5)
    job = tasks.job()(_use_database_on_stop)
    micro = Grelmicro(uses=[tasks, database])
    seen: list[bool] = []

    async with micro:
        submitted = await job.without_owner().submit(
            share(database), share(seen)
        )
        # Act: leaving the app drains the tasks.

    # Assert
    info = await job.without_owner().get(submitted.id)
    assert seen == [True]
    assert info is not None
    assert info.status is JobStatus.SUCCEEDED


async def test_job_base_exception_refuses_submits_while_it_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A submit that arrives while a dying job settles is refused."""
    # Arrange
    settle: Any = MemoryJobAdapter.settle
    refused: list[bool] = []
    tasks = Tasks()
    job = tasks.job()(_abort)

    async def _submits_first(
        self: MemoryJobAdapter, **kwargs: object
    ) -> Settled:
        try:
            await job.without_owner().submit()
        except JobRefusedError:
            refused.append(True)
        return await settle(self, **kwargs)

    monkeypatch.setattr(MemoryJobAdapter, "settle", _submits_first)

    # Act
    with pytest.RaisesGroup(_Abort, flatten_subgroups=True):
        async with tasks:
            await job.without_owner().submit()
            await asyncio.Event().wait()

    # Assert
    assert refused == [True]
