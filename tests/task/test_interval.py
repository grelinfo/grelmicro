"""Test Interval Task."""

import asyncio
from asyncio import sleep
from datetime import datetime, timedelta

import pytest
from pytest_mock import MockFixture

from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
)
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.task._cron import FireInfo, FireOutcome
from grelmicro.task._interval import IntervalTask
from tests.task import samples
from tests.task._helpers import cancel_group, start_task
from tests.task.samples import (
    BadLock,
    WouldBlockLock,
    always_fail,
    notify,
    test1,
)


async def sleep_forever() -> None:
    """Block forever on an unset event."""
    await asyncio.Event().wait()


pytestmark = [pytest.mark.timeout(10)]

SLEEP = 0.01


def test_interval_task_init() -> None:
    """Test Interval Task Initialization."""
    # Act
    task = IntervalTask(seconds=1, function=test1)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_init_with_name() -> None:
    """Test Interval Task Initialization with Name."""
    # Act
    task = IntervalTask(seconds=1, function=test1, name="test1")
    # Assert
    assert task.name == "test1"


def test_interval_task_init_with_seconds_float() -> None:
    """Test Interval Task accepts a number of seconds."""
    # Arrange
    seconds = 5
    # Act
    task = IntervalTask(seconds=seconds, function=test1)
    # Assert
    assert task._seconds == seconds


def test_interval_task_init_with_seconds_timedelta() -> None:
    """Test Interval Task accepts a timedelta and resolves it to seconds."""
    # Arrange
    interval = timedelta(minutes=2)
    # Act
    task = IntervalTask(seconds=interval, function=test1)
    # Assert
    assert task._seconds == interval.total_seconds()


def test_interval_task_init_with_invalid_interval() -> None:
    """Test Interval Task Initialization with Invalid Interval."""
    # Act / Assert
    with pytest.raises(ValueError, match="seconds must be greater than 0"):
        IntervalTask(seconds=0, function=test1)


def test_interval_task_lock_default_name_restamped() -> None:
    """A default-named lock is re-stamped to the task name."""
    lease_duration = 300
    backend = MemoryLockAdapter()
    task = IntervalTask(
        seconds=60,
        function=test1,
        name="cleanup",
        lock=TaskLock(backend=backend, lease_duration=lease_duration),
    )
    task_lock = task._sync_primitives[0]
    assert isinstance(task_lock, TaskLock)
    assert task_lock.name == "cleanup"
    assert task_lock.config.lease_duration == lease_duration


def test_interval_task_lock_explicit_name_honored() -> None:
    """An explicit-named lock keeps its own name."""
    backend = MemoryLockAdapter()
    task = IntervalTask(
        seconds=60,
        function=test1,
        name="cleanup",
        lock=TaskLock("shared", backend=backend, lease_duration=300),
    )
    task_lock = task._sync_primitives[0]
    assert isinstance(task_lock, TaskLock)
    assert task_lock.name == "shared"


def test_interval_task_lock_lease_less_than_seconds_raises() -> None:
    """A lock lease_duration below seconds raises ValueError."""
    backend = MemoryLockAdapter()
    with pytest.raises(
        ValueError,
        match="lease_duration must be greater than or equal to seconds",
    ):
        IntervalTask(
            seconds=60,
            function=test1,
            lock=TaskLock(backend=backend, lease_duration=10),
        )


def test_interval_task_leader_auto_locks() -> None:
    """Leader without an explicit lock auto-configures an interval-aware lock."""
    seconds = 60
    leader = LeaderElection("svc", backend=MemoryLeaderElectionAdapter())
    task = IntervalTask(
        seconds=seconds,
        function=test1,
        name="cleanup",
        leader=leader,
    )
    task_locks = [p for p in task._sync_primitives if isinstance(p, TaskLock)]
    assert len(task_locks) == 1
    assert task_locks[0].name == "cleanup"
    assert task_locks[0].config.lease_duration == seconds * 5
    assert task_locks[0].config.min_hold_duration == seconds


def test_interval_task_local_no_sync() -> None:
    """Neither lock nor leader leaves the task local."""
    task = IntervalTask(seconds=60, function=test1)
    assert task._sync_primitives == []


async def test_interval_task_start() -> None:
    """Test Interval Task Start."""
    # Arrange
    task = IntervalTask(seconds=1, function=notify)
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        cancel_group(tg)


async def test_interval_task_last_fire_outcome() -> None:
    """last_fire.outcome is the FireOutcome.SUCCESS member after a run."""
    task = IntervalTask(seconds=1, function=notify)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        assert task.last_fire is not None
        assert isinstance(task.last_fire, FireInfo)
        assert task.last_fire.outcome is FireOutcome.SUCCESS
        assert isinstance(task.last_fire.outcome, FireOutcome)
        assert task.last_fire.outcome == "success"
        cancel_group(tg)


async def test_interval_task_execution_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Execution Error."""
    # Arrange
    task = IntervalTask(seconds=1, function=always_fail)
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    # Assert
    assert any(
        "Task execution error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_would_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task WouldBlock logs at DEBUG, not ERROR."""
    # Arrange
    caplog.set_level("DEBUG")
    task = IntervalTask(seconds=1, function=notify, sync=WouldBlockLock())

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    # Assert
    assert any(
        "Task skipped:" in record.message
        for record in caplog.records
        if record.levelname == "DEBUG"
    )
    assert not any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_synchronization_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Synchronization Error."""
    # Arrange
    task = IntervalTask(seconds=1, function=notify, sync=BadLock())

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        cancel_group(tg)

    # Assert
    assert any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_stop(
    caplog: pytest.LogCaptureFixture, mocker: MockFixture
) -> None:
    """Test Interval Task stop."""
    # Arrange
    caplog.set_level("INFO")

    class CustomBaseException(BaseException):
        pass

    mocker.patch(
        "grelmicro.task._interval.asyncio.sleep",
        side_effect=CustomBaseException,
    )
    task = IntervalTask(seconds=1, function=test1)

    async def leader_election_during_runtime_error() -> None:
        async with asyncio.TaskGroup() as tg:
            await start_task(tg, task)
            await sleep_forever()

    # Act
    with pytest.raises(BaseExceptionGroup):
        await leader_election_during_runtime_error()

    # Assert
    assert any(
        "Task stopped:" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )


# --- Introspection ---


def test_interval_task_next_fire_time_none_before_start() -> None:
    """next_fire_time is None before the loop starts."""
    task = IntervalTask(seconds=1, function=test1)
    assert task.next_fire_time is None


def test_interval_task_last_fire_none_before_start() -> None:
    """last_fire is None before the first fire."""
    task = IntervalTask(seconds=1, function=test1)
    assert task.last_fire is None


async def test_interval_task_next_fire_time_after_loop_starts() -> None:
    """next_fire_time is a timezone-aware datetime after the loop starts running."""
    task = IntervalTask(seconds=1, function=notify)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        # Give the loop one tick so _last_loop_start is recorded.
        await sleep(SLEEP)
        cancel_group(tg)
    nft = task.next_fire_time
    assert nft is not None
    assert isinstance(nft, datetime)
    assert nft.tzinfo is not None


async def test_interval_task_last_fire_success() -> None:
    """last_fire.outcome is 'success' after a successful run."""
    task = IntervalTask(seconds=1, function=notify)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        assert task.last_fire is not None
        assert isinstance(task.last_fire, FireInfo)
        assert task.last_fire.outcome == "success"
        assert task.last_fire.duration >= 0
        cancel_group(tg)


async def test_interval_task_last_fire_error() -> None:
    """last_fire.outcome is 'error' after a failed run."""
    task = IntervalTask(seconds=1, function=always_fail)
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        assert task.last_fire is not None
        assert task.last_fire.outcome == "error"
        cancel_group(tg)


async def test_interval_task_last_fire_skipped() -> None:
    """last_fire.outcome is 'skipped' when WouldBlockError is raised."""
    task = IntervalTask(seconds=1, function=notify, sync=WouldBlockLock())
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await sleep(SLEEP)
        assert task.last_fire is not None
        assert task.last_fire.outcome == "skipped"
        assert task.last_fire.duration == 0.0
        cancel_group(tg)
