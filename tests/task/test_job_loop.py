"""Test the one store loop a replica runs for all its jobs."""

import asyncio
from collections import Counter
from datetime import timedelta
from types import TracebackType
from typing import Any, Self

import pytest

from grelmicro.clock import VirtualClock
from grelmicro.task import Job, JobStatus, Progress, Tasks
from grelmicro.task.memory import MemoryJobAdapter
from tests.task._job_shared import share, shared

pytestmark = [pytest.mark.timeout(10)]

_SWEEP_SECONDS = 10
"""Long enough for one sweep of the store."""

_FEW_SECONDS = 3
"""Long enough for a few beats, and no sweep."""


class _CountingStore(MemoryJobAdapter):
    """A store that counts each call the replica's loop makes."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: Counter[str] = Counter()

    async def __aenter__(self) -> Self:
        self.calls["open"] += 1
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.calls["close"] += 1
        await super().__aexit__(exc_type, exc_value, traceback)

    async def claim(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls["claim"] += 1
        return await super().claim(**kwargs)

    async def beat(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls["beat"] += 1
        return await super().beat(**kwargs)

    async def reap(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls["reap"] += 1
        return await super().reap(**kwargs)

    async def purge(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls["purge"] += 1
        return await super().purge(**kwargs)


async def _waits(_progress: Progress, gate: str) -> None:
    """Run until the shared `gate` is set."""
    await shared(gate).wait()


async def _noop(_progress: Progress) -> None:
    """Do nothing."""


async def _settled() -> None:
    """Let every ready task run."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _advance(clock: VirtualClock, seconds: int) -> None:
    """Move time forward second by second, so each beat runs."""
    for _ in range(seconds):
        await clock.advance(1)
        await _settled()


def _on_store(store: MemoryJobAdapter, *jobs: Job[...]) -> None:
    """Put `jobs` on one store, as the app's wiring will."""
    for job in jobs:
        job._backend = store


@pytest.mark.parametrize("how_many", [1, 5])
async def test_job_store_calls_do_not_grow_with_the_number_of_jobs(
    how_many: int,
) -> None:
    """A replica makes the same store calls for one job as for many."""
    # Arrange
    clock = VirtualClock()
    store = _CountingStore()
    tasks = Tasks()
    gate = asyncio.Event()
    jobs = [tasks.job(name=f"job-{i}")(_waits) for i in range(how_many)]
    _on_store(store, *jobs)

    async with clock, tasks:
        for job in jobs:
            await job.without_owner().submit(share(gate))
        await _settled()
        store.calls.clear()

        # Act
        await _advance(clock, _SWEEP_SECONDS)
        calls = store.calls.copy()
        gate.set()
        await _settled()

    # Assert
    assert calls == {
        "beat": _SWEEP_SECONDS,
        "reap": 1,
        "purge": 1,
    }


async def test_job_idle_jobs_share_one_claim_a_second() -> None:
    """Jobs with free slots look for queued work in one call a second."""
    # Arrange
    clock = VirtualClock()
    store = _CountingStore()
    tasks = Tasks()
    first = tasks.job(name="first")(_noop)
    second = tasks.job(name="second")(_noop)
    _on_store(store, first, second)

    async with clock, tasks:
        await _settled()
        store.calls.clear()

        # Act
        await _advance(clock, _FEW_SECONDS)

    # Assert
    assert store.calls["claim"] == _FEW_SECONDS


async def test_job_claim_takes_a_job_for_each_name_on_one_store() -> None:
    """A job queued by another replica starts, on either job of the store."""
    # Arrange
    clock = VirtualClock()
    store = MemoryJobAdapter()
    tasks = Tasks()
    first = tasks.job(name="first")(_noop)
    second = tasks.job(name="second")(_noop)
    _on_store(store, first, second)

    async with clock, tasks:
        queued = [
            await _queue_as_another_replica(store, name)
            for name in ("first", "second")
        ]

        # Act
        await _advance(clock, 1)
        infos = [
            await job.without_owner().get(job_id)
            for job, job_id in zip((first, second), queued, strict=True)
        ]

    # Assert
    assert [info.status for info in infos if info is not None] == [
        JobStatus.SUCCEEDED,
        JobStatus.SUCCEEDED,
    ]


async def _queue_as_another_replica(store: MemoryJobAdapter, name: str) -> str:
    """Store a queued job, as a submit on another replica does."""
    submitted = await store.submit(
        name=name,
        job_id=f"{name}-id",
        owner=None,
        payload='{"args": [], "kwargs": {}}',
        context={},
        max_queued=None,
        max_wait=None,
        keep=timedelta(hours=1),
        start_as=None,
        lease=timedelta(seconds=30),
    )
    assert submitted.record is not None
    return submitted.record.id


async def test_job_stopping_keeps_a_job_the_store_just_gave_it() -> None:
    """A stop during a claim still runs what that claim took."""
    # Arrange
    entered, release = asyncio.Event(), asyncio.Event()

    class _SlowClaim(MemoryJobAdapter):
        async def claim(self, **kwargs: Any) -> Any:  # noqa: ANN401
            claimed = await super().claim(**kwargs)
            if claimed:
                entered.set()
                await release.wait()
            return claimed

    clock = VirtualClock()
    store = _SlowClaim()
    tasks = Tasks()
    job = tasks.job(name="job")(_noop)
    _on_store(store, job)

    # Act
    async with clock:
        await tasks.__aenter__()
        job_id = await _queue_as_another_replica(store, "job")
        await clock.advance(1)
        await entered.wait()
        leaving = asyncio.create_task(tasks.__aexit__(None, None, None))
        await _settled()
        release.set()
        await leaving
        info = await store.get(name="job", job_id=job_id, owner=None)

    # Assert
    assert info is not None
    assert info.status is JobStatus.SUCCEEDED


async def test_job_loop_keeps_beating_for_the_jobs_still_running() -> None:
    """A job that stops first leaves the loop to the jobs left on the store."""
    # Arrange
    clock = VirtualClock()
    store = _CountingStore()
    leaving, staying = Tasks(), Tasks()
    gone = leaving.job(name="gone")(_noop)
    kept = staying.job(name="kept")(_waits)
    _on_store(store, gone, kept)
    gate = asyncio.Event()

    async with clock, staying:
        async with leaving:
            await kept.without_owner().submit(share(gate))
            await _settled()
        store.calls.clear()

        # Act
        await _advance(clock, _FEW_SECONDS)
        gate.set()
        await _settled()

    # Assert
    assert store.calls["beat"] == _FEW_SECONDS


async def test_job_store_opens_and_closes_once_for_all_its_jobs() -> None:
    """The first job opens the store, and the last one closes it."""
    # Arrange
    store = _CountingStore()
    tasks = Tasks()
    first = tasks.job(name="first")(_noop)
    second = tasks.job(name="second")(_noop)
    _on_store(store, first, second)

    # Act
    async with tasks:
        during = store.calls.copy()

    # Assert
    assert during["open"] == 1
    assert during["close"] == 0
    assert store.calls["close"] == 1


async def test_job_stopping_claims_nothing_more_on_a_shared_loop() -> None:
    """A draining job leaves its queue for other replicas, with slots free."""
    # Arrange
    clock = VirtualClock()
    store = MemoryJobAdapter()
    leaving, staying = Tasks(), Tasks()
    draining = leaving.job(name="draining", max_concurrent=2)(_waits)
    kept = staying.job(name="kept")(_noop)
    _on_store(store, draining, kept)
    gate = asyncio.Event()

    async with clock, staying:
        await leaving.__aenter__()
        await draining.without_owner().submit(share(gate))
        stopped = asyncio.create_task(leaving.__aexit__(None, None, None))
        await _settled()
        job_id = await _queue_as_another_replica(store, "draining")

        # Act
        await _advance(clock, 1)
        info = await store.get(name="draining", job_id=job_id, owner=None)
        gate.set()
        await stopped

    # Assert
    assert info is not None
    assert info.status is JobStatus.QUEUED


async def test_job_with_a_name_already_on_the_store_is_refused() -> None:
    """Two jobs with one name on one store cannot share its loop."""
    # Arrange
    store = MemoryJobAdapter()
    one, other = Tasks(), Tasks()
    first = one.job(name="same")(_noop)
    second = other.job(name="same")(_noop)
    _on_store(store, first, second)

    # Act / Assert
    async with one:
        with pytest.raises(ValueError, match="same"):
            await second()


async def test_job_store_error_in_the_loop_reaches_the_last_job_to_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store error that ended the loop is raised, not lost."""

    # Arrange
    async def _fails(_self: object, **_kwargs: object) -> None:
        msg = "store down"
        raise RuntimeError(msg)

    monkeypatch.setattr(MemoryJobAdapter, "claim", _fails)
    clock = VirtualClock()
    job = Tasks().job(name="job")(_noop)
    stop = asyncio.Event()

    # Act / Assert
    async with clock:
        running = asyncio.create_task(job(stop=stop))
        await _settled()
        await _advance(clock, 1)
        stop.set()
        with pytest.RaisesGroup(RuntimeError, flatten_subgroups=True):
            await running
