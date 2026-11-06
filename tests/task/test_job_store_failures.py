"""Test how jobs ride out a store that fails or answers late."""

import asyncio
import logging
from collections import Counter
from typing import Any

import pytest

from grelmicro.clock import VirtualClock
from grelmicro.task import Job, JobInfo, JobStatus, Progress, Tasks
from grelmicro.task._job import _info
from grelmicro.task.errors import JobRefusedError
from grelmicro.task.memory import MemoryJobAdapter
from tests.task._job_shared import share, shared

pytestmark = [pytest.mark.timeout(10)]

_PAST_THE_LEASE = 31
"""Seconds past the 30 second lease."""

_FEW_BEATS = 3
"""Seconds for a few beats."""

_END_WRITES = 2
"""End writes of a job: one from its run, one more at stop."""


class _FlakyStore(MemoryJobAdapter):
    """A store whose calls fail, or wait, when a test says so."""

    def __init__(self) -> None:
        super().__init__()
        self.failing: set[str] = set()
        """Calls that fail until removed."""
        self.fail_next: Counter[str] = Counter()
        """Calls that fail that many more times."""
        self.gates: dict[str, asyncio.Event] = {}
        """Calls that wait for their event before they run."""
        self.answered: Counter[str] = Counter()
        """Calls the store answered."""

    async def _call(self, call: str, **kwargs: Any) -> Any:  # noqa: ANN401
        gate = self.gates.get(call)
        if gate is not None:
            await gate.wait()
        if call in self.failing or self.fail_next[call] > 0:
            self.fail_next[call] -= 1
            msg = "store down"
            raise ConnectionError(msg)
        answer = await getattr(super(), call)(**kwargs)
        self.answered[call] += 1
        return answer

    async def submit(self, **kwargs: Any) -> Any:  # noqa: ANN401
        return await self._call("submit", **kwargs)

    async def claim(self, **kwargs: Any) -> Any:  # noqa: ANN401
        return await self._call("claim", **kwargs)

    async def beat(self, **kwargs: Any) -> Any:  # noqa: ANN401
        return await self._call("beat", **kwargs)

    async def settle(self, **kwargs: Any) -> Any:  # noqa: ANN401
        return await self._call("settle", **kwargs)

    async def reap(self, **kwargs: Any) -> Any:  # noqa: ANN401
        return await self._call("reap", **kwargs)


async def _waits(progress: Progress, gate: str) -> int:
    """Report 3 items, then return 42 once the shared `gate` is set."""
    progress.advance(3)
    await shared(gate).wait()
    return 42


async def _records_cancel(_progress: Progress, gate: str, seen: str) -> None:
    """Wait for `gate`, and note in `seen` when the wait is cancelled."""
    try:
        await shared(gate).wait()
    except asyncio.CancelledError:
        shared(seen).append("cancelled")
        raise


async def _notes_stopping(progress: Progress, seen: str) -> None:
    """Note in `seen` whether the job was asked to stop."""
    shared(seen).append(progress.stopping)


async def _settled() -> None:
    """Let every ready task run."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _advance(clock: VirtualClock, seconds: int) -> None:
    """Move time forward second by second, so each beat runs."""
    for _ in range(seconds):
        await clock.advance(1)
        await _settled()


def _job(tasks: Tasks, store: MemoryJobAdapter, **options: Any) -> Job[...]:  # noqa: ANN401
    """Return a job of `tasks` on `store`, as the app's wiring will."""
    job = tasks.job(name="import", **options)(_waits)
    job._backend = store
    return job


async def _read(store: MemoryJobAdapter, job_id: str) -> JobInfo:
    """Return the job as the store has it."""
    record = await MemoryJobAdapter.get(
        store, name="import", job_id=job_id, owner=None
    )
    assert record is not None
    return _info(record)


async def test_job_beats_again_once_the_store_answers() -> None:
    """A beat that fails is tried again at the next beat."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store)
    gate = asyncio.Event()

    async with clock:
        async with tasks:
            submitted = await job.without_owner().submit(share(gate))
            store.failing.add("beat")

            # Act
            await _advance(clock, 3)
            store.failing.clear()
            store.answered.clear()
            await _advance(clock, _FEW_BEATS)
            beats = store.answered["beat"]
            gate.set()
            await _settled()

        # Assert
        assert beats == _FEW_BEATS
        info = await _read(store, submitted.id)
        assert (info.status, info.result) == (JobStatus.SUCCEEDED, 42)


async def test_job_store_out_past_the_lease_stops_the_body() -> None:
    """A replica that cannot beat for a whole lease stops its bodies."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = tasks.job(name="import")(_records_cancel)
    job._backend = store
    seen: list[str] = []

    async with clock:
        async with tasks:
            submitted = await job.without_owner().submit(
                share(asyncio.Event()), share(seen)
            )
            store.failing.add("beat")

            # Act
            await _advance(clock, _PAST_THE_LEASE)

        # Assert
        assert seen == ["cancelled"]
        info = await _read(store, submitted.id)
        assert (info.status, info.error) == (JobStatus.FAILED, "Job lost")


async def test_job_sweep_error_leaves_the_loop_beating(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sweep that fails logs a warning, and the beats go on."""
    # Arrange
    caplog.set_level(logging.WARNING, logger="grelmicro.task")
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store)
    gate = asyncio.Event()
    store.failing.add("reap")

    async with clock, tasks:
        await job.without_owner().submit(share(gate))

        # Act
        await _advance(clock, 10)
        store.answered.clear()
        await _advance(clock, _FEW_BEATS)
        beats = store.answered["beat"]
        gate.set()

    # Assert
    assert beats == _FEW_BEATS
    assert "Job store sweep failed" in caplog.text


async def test_job_claim_error_gives_the_slots_back() -> None:
    """A claim that fails frees its slots, and a later claim runs the job."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store, max_concurrent=1)
    first, second = asyncio.Event(), asyncio.Event()
    second.set()

    async with clock:
        async with tasks:
            await job.without_owner().submit(share(first))
            queued = await job.without_owner().submit(share(second))
            store.failing.add("claim")

            # Act
            first.set()
            await _advance(clock, 2)
            store.failing.clear()
            await _advance(clock, 2)

        # Assert
        info = await _read(store, queued.id)
        assert (info.status, info.result) == (JobStatus.SUCCEEDED, 42)


async def test_job_settle_error_is_tried_again_and_keeps_the_result() -> None:
    """A job whose end the store missed ends as it ran, once the store answers."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store)
    gate = asyncio.Event()
    store.fail_next["settle"] = 2

    async with clock:
        async with tasks:
            submitted = await job.without_owner().submit(share(gate))

            # Act
            gate.set()
            await _advance(clock, 3)

        # Assert
        info = await _read(store, submitted.id)
        assert (info.status, info.result, info.done) == (
            JobStatus.SUCCEEDED,
            42,
            3,
        )


async def test_job_settle_error_past_the_lease_loses_only_that_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A job the store never takes the end of is lost, and the replica goes on."""
    # Arrange
    caplog.set_level(logging.WARNING, logger="grelmicro.task")
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store)
    gate = asyncio.Event()
    gate.set()

    async with clock:
        async with tasks:
            store.failing.add("settle")
            lost = await job.without_owner().submit(share(gate))

            # Act
            await _advance(clock, _PAST_THE_LEASE)
            store.failing.clear()
            await _advance(clock, _PAST_THE_LEASE)
            later = await job.without_owner().submit(share(gate))
            await _settled()

        # Assert
        assert "Job could not be ended in its store: import" in caplog.text
        info = await _read(store, lost.id)
        assert (info.status, info.error) == (JobStatus.FAILED, "Job lost")
        info = await _read(store, later.id)
        assert info.status is JobStatus.SUCCEEDED


async def test_job_counts_reach_the_store_after_a_failed_beat() -> None:
    """Counts a failed beat did not write go with the next one."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store)
    gate = asyncio.Event()

    async with clock:
        async with tasks:
            submitted = await job.without_owner().submit(share(gate))
            await _settled()
            store.fail_next["beat"] = 1

            # Act
            await _advance(clock, 2)
            info = await _read(store, submitted.id)
            gate.set()

        # Assert
        assert (info.status, info.done) == (JobStatus.RUNNING, 3)


async def test_job_submit_in_flight_at_stop_runs_its_body() -> None:
    """A submit the store answers after stop still runs the job it started."""
    # Arrange
    store = _FlakyStore()
    tasks = Tasks()
    job = tasks.job(name="import", max_concurrent=2)(_notes_stopping)
    job._backend = store
    seen: list[bool] = []
    answer = store.gates["submit"] = asyncio.Event()

    async def _answer_later() -> None:
        await _settled()
        answer.set()

    # Act
    async with tasks:
        submitting = [
            asyncio.create_task(job.without_owner().submit(share(seen)))
            for _ in range(2)
        ]
        await _settled()
        answering = asyncio.create_task(_answer_later())
    submitted = [await submit for submit in submitting]
    await answering

    # Assert
    assert [info.status for info in submitted] == [JobStatus.RUNNING] * 2
    assert seen == [True, True]
    for info in submitted:
        ended = await _read(store, info.id)
        assert ended.status is JobStatus.SUCCEEDED


async def test_job_task_cancelled_before_its_first_step_ends_cancelled() -> (
    None
):
    """A job task cancelled before it ran ends `cancelled`, not `running`."""
    # Arrange
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store)

    async with tasks:
        submitted = await job.without_owner().submit(share(asyncio.Event()))

        # Act
        task = job._runs[submitted.id].task
        assert task is not None
        task.cancel()

    # Assert
    info = await _read(store, submitted.id)
    assert info.status is JobStatus.CANCELLED


async def test_job_submit_waits_for_a_claim_in_flight_instead_of_refusing() -> (
    None
):
    """An idle replica runs a submit at once, even while a claim is out."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store, max_queued=0)
    gate = asyncio.Event()
    gate.set()
    claim = store.gates["claim"] = asyncio.Event()

    async with clock, tasks:
        await _advance(clock, 1)
        submitting = asyncio.create_task(
            job.without_owner().submit(share(gate))
        )
        await _settled()

        # Act
        claim.set()
        submitted = await submitting
        await _settled()

    # Assert
    assert submitted.status is JobStatus.RUNNING


async def test_job_submit_answered_after_a_forced_stop_is_refused() -> None:
    """A forced stop does not wait for the store, and the late submit is refused."""
    # Arrange
    store = _FlakyStore()
    tasks = Tasks(shutdown_timeout=0)
    job = _job(tasks, store)
    answer = store.gates["submit"] = asyncio.Event()

    async with tasks:
        submitting = asyncio.create_task(
            job.without_owner().submit(share(asyncio.Event()))
        )
        await _settled()

    # Act
    answer.set()

    # Assert
    with pytest.raises(JobRefusedError, match="shutting down"):
        await submitting
    assert (job._runs, job._running) == ({}, 0)


async def test_job_beat_answered_after_the_job_ended_changes_nothing() -> None:
    """A beat that comes back after its job ended leaves that end as it is."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store)
    gate = asyncio.Event()
    beat = store.gates["beat"] = asyncio.Event()

    async with clock:
        async with tasks:
            submitted = await job.without_owner().submit(share(gate))
            await _advance(clock, 1)
            gate.set()
            await _settled()

            # Act
            beat.set()
            await _settled()

        # Assert
        info = await _read(store, submitted.id)
    assert (info.status, info.result) == (JobStatus.SUCCEEDED, 42)


async def test_job_end_cut_by_a_forced_stop_is_written_once_more() -> None:
    """A forced stop during the end write tries it once more, as it ended."""
    # Arrange
    store = _FlakyStore()
    tasks = Tasks(shutdown_timeout=0)
    job = _job(tasks, store)
    gate = asyncio.Event()
    gate.set()
    store.fail_next["settle"] = 1

    # Act
    async with tasks:
        submitted = await job.without_owner().submit(share(gate))
        await _settled()

    # Assert
    info = await _read(store, submitted.id)
    assert (info.status, info.result) == (JobStatus.SUCCEEDED, 42)
    assert not job._runs


class _StoreCrash(BaseException):
    """A crash that is not an error, such as an interrupt."""


class _CrashingStore(MemoryJobAdapter):
    """A store whose end write crashes."""

    def __init__(self) -> None:
        super().__init__()
        self.settles = 0

    async def settle(self, **_kwargs: Any) -> bool:  # noqa: ANN401
        self.settles += 1
        raise _StoreCrash


async def test_job_store_crash_at_the_end_stops_the_replica() -> None:
    """A crash out of the end write propagates, as one from the body does."""
    # Arrange
    tasks = Tasks()
    store = _CrashingStore()
    job = _job(tasks, store)
    gate = asyncio.Event()
    gate.set()

    # Act
    with pytest.RaisesGroup(_StoreCrash, flatten_subgroups=True):
        async with tasks:
            await job.without_owner().submit(share(gate))
            await asyncio.Event().wait()

    # Assert
    assert store.settles == _END_WRITES


async def test_job_cancel_while_the_end_is_retried_keeps_the_end() -> None:
    """A cancel that comes after the body returned does not cut its end."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store)
    gate = asyncio.Event()
    gate.set()
    store.fail_next["settle"] = 2

    async with clock, tasks:
        submitted = await job.without_owner().submit(share(gate))
        await _settled()

        # Act
        await job.without_owner().cancel(submitted.id)
        await _advance(clock, 3)
        info = await _read(store, submitted.id)

    # Assert
    assert (info.status, info.result) == (JobStatus.SUCCEEDED, 42)


async def test_job_full_replica_queues_a_submit_after_an_empty_claim() -> None:
    """A claim that found nothing leaves no slot reserved."""
    # Arrange
    clock = VirtualClock()
    store = _FlakyStore()
    tasks = Tasks()
    job = _job(tasks, store, max_concurrent=1)
    gate = asyncio.Event()

    async with clock, tasks:
        await _advance(clock, 1)
        await job.without_owner().submit(share(gate))

        # Act
        queued = await job.without_owner().submit(share(gate))
        gate.set()

    # Assert
    assert queued.status is JobStatus.QUEUED
