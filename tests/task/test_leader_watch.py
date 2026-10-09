"""Test the report on a task gated on a leader election that does not run."""

import asyncio
import logging
import warnings
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta

import pytest
from fastapi import FastAPI
from pytest_mock import MockFixture

from grelmicro import Grelmicro, LeaderNotRunningWarning
from grelmicro.clock import VirtualClock
from grelmicro.coordination import Coordination, LeaderElection
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.task import FireOutcome, Tasks
from grelmicro.task._cron import CronTask
from grelmicro.task._fire import FireRecorder
from tests.task import samples

pytestmark = [pytest.mark.timeout(10)]

LEASE = 15.0
"""The default lease duration of a `LeaderElection`, in seconds."""

FIRES = 0.1
"""Real seconds that cover several fires of a task every 0.01 seconds."""


async def _gated_work() -> None:
    """Do nothing, gated on a leader election."""


def _gated(election: LeaderElection) -> Tasks:
    """Return a Tasks running `_gated_work` every 0.01s, gated on `election`."""
    tasks = Tasks()
    tasks.every(interval=timedelta(milliseconds=10), gate=election)(_gated_work)
    return tasks


def _app(clock: VirtualClock, *uses: Tasks) -> Grelmicro:
    """Build an app on `clock` with memory coordination and `uses`."""
    return Grelmicro(
        uses=[
            clock,
            Coordination(
                lock=MemoryLockAdapter(),
                leaderelection=MemoryLeaderElectionAdapter(),
                schedule=MemoryScheduleAdapter(),
            ),
            *uses,
        ]
    )


@contextmanager
def _reports() -> Iterator[list[warnings.WarningMessage]]:
    """Record every leader-not-running warning instead of raising it."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", LeaderNotRunningWarning)
        yield caught


async def _wait_for(caught: list[warnings.WarningMessage]) -> None:
    """Wait until a warning is recorded."""
    for _ in range(500):
        if caught:
            return
        await asyncio.sleep(0.01)
    pytest.fail("no leader-not-running warning was recorded")


@asynccontextmanager
async def _running(election: LeaderElection) -> AsyncIterator[None]:
    """Drive `election` by hand for the duration of the block."""
    stop = asyncio.Event()
    async with asyncio.TaskGroup() as tg:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tg.create_task(election(ready=ready, stop=stop))
        await ready
        yield
        stop.set()


async def test_election_never_run_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An election nothing runs is reported once a lease duration has passed."""
    clock = VirtualClock()
    election = LeaderElection("svc")

    with _reports() as caught:
        async with _app(clock, _gated(election)):
            await asyncio.sleep(FIRES)
            assert not caught
            await clock.advance(LEASE)
            await _wait_for(caught)

    [report] = caught
    message = str(report.message)
    assert "'tests.task.test_leader_watch:_gated_work'" in message
    assert "'svc'" in message
    assert "tasks.add_task(election)" in message
    assert "[leader-not-running]" in message
    [record] = [
        r
        for r in caplog.records
        if getattr(r, "diagnostic", None) == "leader-not-running"
    ]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == message


async def test_election_never_run_warns_once_across_many_fires() -> None:
    """The report is made once, however many fires the task skips after it."""
    clock = VirtualClock()
    election = LeaderElection("svc")

    with _reports() as caught:
        async with _app(clock, _gated(election)):
            await clock.advance(LEASE)
            await _wait_for(caught)
            for _ in range(3):
                await clock.advance(LEASE)
                await asyncio.sleep(FIRES)

    assert len(caught) == 1


async def test_cron_task_on_an_election_never_run_warns() -> None:
    """A cron task reports the election on the fire it skips."""
    clock = VirtualClock()
    election = LeaderElection("svc")
    tasks = Tasks()
    tasks.cron("* * * * *", gate=election)(_gated_work)
    [watched] = tasks.tasks
    assert isinstance(watched, CronTask)

    with _reports() as caught:
        async with _app(clock, tasks):
            await clock.advance(LEASE)
            # Drive one scheduled skip, instead of waiting for the minute.
            await watched._tick_guarded(catchup=False)

    assert len(caught) == 1


async def test_election_started_within_the_lease_does_not_warn() -> None:
    """An election started late, but within a lease duration, is never reported."""
    clock = VirtualClock()
    election = LeaderElection("svc")

    with _reports() as caught:
        async with _app(clock, _gated(election)):
            await clock.advance(LEASE / 2)
            await asyncio.sleep(FIRES)
            async with _running(election):
                await asyncio.sleep(FIRES)
                await clock.advance(LEASE * 2)
                await asyncio.sleep(FIRES)

    assert not caught


async def test_election_run_by_tasks_outside_the_app_does_not_warn() -> None:
    """An election run by a Tasks the app does not hold is never reported."""
    clock = VirtualClock()
    election = LeaderElection("svc")

    with _reports() as caught:
        async with _app(clock, _gated(election)), Tasks(tasks=[election]):
            await asyncio.sleep(FIRES)
            await clock.advance(LEASE * 2)
            await asyncio.sleep(FIRES)

    assert not caught


async def test_election_run_by_a_later_lifespan_does_not_warn() -> None:
    """An election a lifespan starts after the app opened is never reported."""
    clock = VirtualClock()
    election = LeaderElection("svc", backend=MemoryLeaderElectionAdapter())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with Tasks(tasks=[election]):
            yield

    app = FastAPI(lifespan=lifespan)
    _app(clock, _gated(election)).install(app)

    with _reports() as caught:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(FIRES)
            assert election.is_running()
            await clock.advance(LEASE * 2)
            await asyncio.sleep(FIRES)

    assert not caught


async def test_interval_report_as_error_keeps_every_task_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warnings filter set to error ends neither the gated task nor others."""
    clock = VirtualClock()
    election = LeaderElection("svc")
    tasks = _gated(election)
    tasks.every(interval=timedelta(milliseconds=10))(samples.count_execution)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        async with _app(clock, tasks):
            await clock.advance(LEASE)
            await asyncio.sleep(FIRES)
            counted = samples.execution_count
            await asyncio.sleep(FIRES)
            assert not any(handle.done() for handle in tasks._task_handles)
            assert samples.execution_count > counted

    reports = [
        r
        for r in caplog.records
        if getattr(r, "diagnostic", None) == "leader-not-running"
    ]
    assert len(reports) == 1


async def test_cron_report_as_error_records_the_skip_once(
    mocker: MockFixture,
) -> None:
    """A warnings filter set to error leaves one skipped fire, not an error."""
    clock = VirtualClock()
    election = LeaderElection("svc")
    tasks = Tasks()
    tasks.cron("* * * * *", gate=election)(_gated_work)
    tasks.every(interval=timedelta(milliseconds=10))(samples.count_execution)
    watched = tasks.tasks[0]
    assert isinstance(watched, CronTask)
    unrun = mocker.spy(FireRecorder, "unrun")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        async with _app(clock, tasks):
            await clock.advance(LEASE)
            unrun.reset_mock()
            await watched._tick_guarded(catchup=False)
            outcomes = [
                call.args[2]
                for call in unrun.call_args_list
                if call.args[0] is watched._fire
            ]
            counted = samples.execution_count
            await asyncio.sleep(FIRES)
            assert samples.execution_count > counted

    assert outcomes == [FireOutcome.SKIPPED]
    assert watched.last_fire is not None
    assert watched.last_fire.outcome == FireOutcome.SKIPPED
