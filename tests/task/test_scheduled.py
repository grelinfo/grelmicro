"""Test Scheduled Task (IntervalTask with distributed lock)."""

import asyncio
from asyncio import sleep

import pytest
from pytest_mock import MockFixture

from grelmicro.coordination._protocol import LeaderElectionBackend, LockBackend
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
)
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.task._interval import IntervalTask
from tests.task import samples
from tests.task._helpers import cancel_group, start_task
from tests.task.samples import (
    always_fail,
    notify,
    test1,
)


async def sleep_forever() -> None:
    """Block forever on an unset event."""
    await asyncio.Event().wait()


pytestmark = [pytest.mark.timeout(10)]

SECONDS = 0.1
SLEEP = 0.01


def test_interval_task_with_lock_init() -> None:
    """Test IntervalTask with lock initialization."""
    # Arrange
    backend = MemoryLockAdapter()
    # Act
    task = IntervalTask(
        seconds=1,
        function=test1,
        lock=TaskLock(backend=backend, lease_duration=5),
    )
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_with_lock_init_with_name() -> None:
    """Test IntervalTask with lock initialization with name."""
    # Arrange
    backend = MemoryLockAdapter()
    # Act
    task = IntervalTask(
        seconds=1,
        function=test1,
        name="my-task",
        lock=TaskLock(backend=backend, lease_duration=5),
    )
    # Assert
    assert task.name == "my-task"


def test_interval_task_with_lock_init_invalid_seconds() -> None:
    """Test IntervalTask with lock initialization with invalid seconds."""
    # Arrange
    backend = MemoryLockAdapter()
    # Act / Assert
    with pytest.raises(ValueError, match="seconds must be greater than 0"):
        IntervalTask(
            seconds=0,
            function=test1,
            lock=TaskLock(backend=backend, lease_duration=5),
        )


def test_interval_task_with_lock_default_lease_duration() -> None:
    """Test IntervalTask with leader uses default lease_duration."""
    # Arrange
    leader = LeaderElection(
        "test-leader", backend=MemoryLeaderElectionAdapter()
    )
    # Act - leader implies lock, lease_duration defaults to interval * 5
    task = IntervalTask(seconds=10, function=test1, leader=leader)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_with_lock_custom_lease_duration() -> None:
    """Test IntervalTask with custom lease_duration."""
    # Arrange
    backend = MemoryLockAdapter()
    # Act
    task = IntervalTask(
        seconds=10,
        function=test1,
        lock=TaskLock(backend=backend, lease_duration=100),
    )
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_with_lease_duration_validation() -> None:
    """Test IntervalTask lease_duration validation."""
    # Arrange
    backend = MemoryLockAdapter()
    # Act / Assert
    with pytest.raises(
        ValueError,
        match="lease_duration must be greater than or equal to seconds",
    ):
        IntervalTask(
            seconds=10,
            function=test1,
            lock=TaskLock(backend=backend, lease_duration=5),
        )


def test_tasklock_min_hold_duration_validation() -> None:
    """Test TaskLock rejects min_hold_duration greater than lease_duration."""
    backend = MemoryLockAdapter()
    with pytest.raises(
        ValueError,
        match="min_hold_duration must be less than or equal to lease_duration",
    ):
        TaskLock(backend=backend, lease_duration=20, min_hold_duration=25)


async def test_interval_task_with_lock_and_resource_lock(
    backend: LockBackend,
) -> None:
    """Test IntervalTask with Lock (resource sync) + distributed lock."""
    resource_lock = Lock(name="shared-resource", backend=backend)
    task = IntervalTask(
        seconds=SECONDS,
        function=notify,
        lock=TaskLock(
            backend=backend,
            lease_duration=SECONDS * 5,
            min_hold_duration=SECONDS,
        ),
        sync=resource_lock,
    )
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        cancel_group(tg)


def test_interval_task_custom_min_hold_duration() -> None:
    """Test IntervalTask with custom min_hold_duration."""
    backend = MemoryLockAdapter()
    # Act - should not raise
    task = IntervalTask(
        seconds=10,
        function=test1,
        lock=TaskLock(backend=backend, lease_duration=100, min_hold_duration=5),
    )
    assert task.name == "tests.task.samples:test1"


async def test_interval_task_with_lock_start(backend: LockBackend) -> None:
    """Test IntervalTask with lock start."""
    # Arrange
    task = IntervalTask(
        seconds=SECONDS,
        function=notify,
        lock=TaskLock(
            backend=backend,
            lease_duration=SECONDS * 5,
            min_hold_duration=SECONDS,
        ),
    )
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        async with samples.condition:
            await samples.condition.wait()
        cancel_group(tg)


async def test_interval_task_with_lock_execution_error(
    backend: LockBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test IntervalTask with lock execution error."""
    # Arrange
    task = IntervalTask(
        seconds=SECONDS,
        function=always_fail,
        lock=TaskLock(
            backend=backend,
            lease_duration=SECONDS * 5,
            min_hold_duration=SECONDS,
        ),
    )
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


async def test_interval_task_with_lock_synchronization_error(
    backend: LockBackend,
    caplog: pytest.LogCaptureFixture,
    mocker: MockFixture,
) -> None:
    """Test IntervalTask with lock synchronization error."""
    # Arrange
    task = IntervalTask(
        seconds=SECONDS,
        function=notify,
        lock=TaskLock(
            backend=backend,
            lease_duration=SECONDS * 5,
            min_hold_duration=SECONDS,
        ),
    )
    mocker.patch.object(
        backend, "acquire", side_effect=RuntimeError("backend down")
    )

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


async def test_interval_task_with_lock_stop(
    backend: LockBackend,
    caplog: pytest.LogCaptureFixture,
    mocker: MockFixture,
) -> None:
    """Test IntervalTask with lock stop."""
    # Arrange
    caplog.set_level("INFO")

    class CustomBaseException(BaseException):
        pass

    mocker.patch(
        "grelmicro.task._interval.asyncio.sleep",
        side_effect=CustomBaseException,
    )
    task = IntervalTask(
        seconds=1,
        function=test1,
        lock=TaskLock(backend=backend, lease_duration=5),
    )

    async def task_during_runtime_error() -> None:
        async with asyncio.TaskGroup() as tg:
            await start_task(tg, task)
            await sleep_forever()

    # Act
    with pytest.raises(BaseExceptionGroup):
        await task_during_runtime_error()

    # Assert
    assert any(
        "Task stopped:" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )


# --- End-to-end: Leader election tests (unique to new API) ---


async def test_interval_task_with_leader_executes(
    backend: LockBackend,
    leader_backend: LeaderElectionBackend,
) -> None:
    """Test IntervalTask executes when worker is leader."""
    # Arrange
    leader = LeaderElection(
        "test-leader", backend=leader_backend, worker="worker_1"
    )
    task = IntervalTask(
        seconds=SECONDS,
        function=samples.set_event_1,
        name="e2e_task",
        leader=leader,
        lock=TaskLock(
            backend=backend,
            worker="worker_1",
            lease_duration=SECONDS * 5,
            min_hold_duration=SECONDS,
        ),
    )

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader)
        await start_task(tg, task)
        await samples.e2e_event_1.wait()
        cancel_group(tg)


async def test_interval_task_with_leader_skips_when_not_leader(
    backend: LockBackend,
    leader_backend: LeaderElectionBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test IntervalTask skips when worker is not leader."""
    # Arrange
    caplog.set_level("DEBUG")
    leader_1 = LeaderElection(
        "test-leader", backend=leader_backend, worker="worker_1"
    )
    leader_2 = LeaderElection(
        "test-leader", backend=leader_backend, worker="worker_2"
    )
    task = IntervalTask(
        seconds=SECONDS,
        function=samples.set_event_1,
        name="e2e_task",
        leader=leader_2,
        lock=TaskLock(
            backend=backend,
            worker="worker_2",
            lease_duration=SECONDS * 5,
            min_hold_duration=SECONDS,
        ),
    )

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_1)
        await start_task(tg, leader_2)
        await start_task(tg, task)
        await sleep(SECONDS * 3)
        cancel_group(tg)

    # Assert
    assert not samples.e2e_event_1.is_set()
    assert any(
        "Task skipped:" in record.message
        for record in caplog.records
        if record.levelname == "DEBUG"
    )
