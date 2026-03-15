"""Test Scheduled Task (IntervalTask with distributed lock)."""

from collections.abc import AsyncGenerator

import pytest
from anyio import Event, create_task_group, sleep, sleep_forever
from pytest_mock import MockFixture

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.leaderelection import LeaderElection
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.task._interval import IntervalTask
from tests.task import samples
from tests.task.samples import (
    always_fail,
    condition,
    notify,
    test1,
)

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]

SECONDS = 0.1
SLEEP = 0.01


@pytest.fixture
async def backend() -> AsyncGenerator[SyncBackend]:
    """Return Memory Synchronization Backend."""
    async with MemorySyncBackend() as backend:
        yield backend


def test_interval_task_with_lock_init() -> None:
    """Test IntervalTask with lock initialization."""
    # Arrange
    backend = MemorySyncBackend()
    # Act
    task = IntervalTask(
        interval=1, function=test1, lock_at_most_for=5, backend=backend
    )
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_with_lock_init_with_name() -> None:
    """Test IntervalTask with lock initialization with name."""
    # Arrange
    backend = MemorySyncBackend()
    # Act
    task = IntervalTask(
        interval=1,
        function=test1,
        name="my-task",
        lock_at_most_for=5,
        backend=backend,
    )
    # Assert
    assert task.name == "my-task"


def test_interval_task_with_lock_init_invalid_seconds() -> None:
    """Test IntervalTask with lock initialization with invalid seconds."""
    # Arrange
    backend = MemorySyncBackend()
    # Act / Assert
    with pytest.raises(ValueError, match="Interval must be greater than 0"):
        IntervalTask(
            interval=0, function=test1, lock_at_most_for=5, backend=backend
        )


def test_interval_task_with_lock_default_lock_at_most_for() -> None:
    """Test IntervalTask with leader uses default lock_at_most_for."""
    # Arrange
    backend = MemorySyncBackend()
    leader = LeaderElection("test-leader", backend=backend)
    # Act — leader implies lock, lock_at_most_for defaults to interval * 5
    task = IntervalTask(
        interval=10, function=test1, leader=leader, backend=backend
    )
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_with_lock_custom_lock_at_most_for() -> None:
    """Test IntervalTask with custom lock_at_most_for."""
    # Arrange
    backend = MemorySyncBackend()
    # Act
    task = IntervalTask(
        interval=10, function=test1, lock_at_most_for=100, backend=backend
    )
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_with_lock_at_most_for_validation() -> None:
    """Test IntervalTask lock_at_most_for validation."""
    # Arrange
    backend = MemorySyncBackend()
    # Act / Assert
    with pytest.raises(
        ValueError,
        match="lock_at_most_for must be greater than or equal to interval",
    ):
        IntervalTask(
            interval=10, function=test1, lock_at_most_for=5, backend=backend
        )


def test_interval_task_lock_at_least_for_without_lock() -> None:
    """Test lock_at_least_for requires lock_at_most_for or leader."""
    with pytest.raises(
        ValueError,
        match="lock_at_least_for requires lock_at_most_for or leader",
    ):
        IntervalTask(
            interval=10, function=test1, lock_at_least_for=5
        )


def test_interval_task_lock_at_least_for_validation() -> None:
    """Test lock_at_least_for must be <= lock_at_most_for."""
    backend = MemorySyncBackend()
    with pytest.raises(
        ValueError,
        match="lock_at_least_for must be less than or equal to lock_at_most_for",
    ):
        IntervalTask(
            interval=10,
            function=test1,
            lock_at_most_for=20,
            lock_at_least_for=25,
            backend=backend,
        )


def test_interval_task_custom_lock_at_least_for() -> None:
    """Test IntervalTask with custom lock_at_least_for."""
    backend = MemorySyncBackend()
    # Act — should not raise
    task = IntervalTask(
        interval=10,
        function=test1,
        lock_at_most_for=100,
        lock_at_least_for=5,
        backend=backend,
    )
    assert task.name == "tests.task.samples:test1"


async def test_interval_task_with_lock_start(backend: SyncBackend) -> None:
    """Test IntervalTask with lock start."""
    # Arrange
    task = IntervalTask(
        interval=SECONDS,
        function=notify,
        lock_at_most_for=SECONDS * 5,
        backend=backend,
    )
    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        async with condition:
            await condition.wait()
        tg.cancel_scope.cancel()


async def test_interval_task_with_lock_execution_error(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test IntervalTask with lock execution error."""
    # Arrange
    task = IntervalTask(
        interval=SECONDS,
        function=always_fail,
        lock_at_most_for=SECONDS * 5,
        backend=backend,
    )
    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        await sleep(SLEEP)
        tg.cancel_scope.cancel()

    # Assert
    assert any(
        "Task execution error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_with_lock_synchronization_error(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
    mocker: MockFixture,
) -> None:
    """Test IntervalTask with lock synchronization error."""
    # Arrange
    task = IntervalTask(
        interval=SECONDS,
        function=notify,
        lock_at_most_for=SECONDS * 5,
        backend=backend,
    )
    mocker.patch.object(
        backend, "acquire", side_effect=RuntimeError("backend down")
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        await sleep(SLEEP)
        tg.cancel_scope.cancel()

    # Assert
    assert any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_with_lock_stop(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
    mocker: MockFixture,
) -> None:
    """Test IntervalTask with lock stop."""
    # Arrange
    caplog.set_level("INFO")

    class CustomBaseException(BaseException):
        pass

    mocker.patch(
        "grelmicro.task._interval.sleep", side_effect=CustomBaseException
    )
    task = IntervalTask(
        interval=1,
        function=test1,
        lock_at_most_for=5,
        backend=backend,
    )

    async def task_during_runtime_error() -> None:
        async with create_task_group() as tg:
            await tg.start(task)
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


# --- End-to-end: IntervalTask with distributed lock ---


@pytest.fixture(autouse=True)
def _reset_e2e_state() -> None:
    """Reset shared e2e state before each test."""
    samples.e2e_event_1 = Event()
    samples.e2e_event_2 = Event()
    samples.e2e_counter = {"worker_1": 0, "worker_2": 0}


async def test_interval_task_with_lock_two_workers(
    backend: SyncBackend,
) -> None:
    """Test only one worker executes when both use lock."""
    # Arrange
    task_1 = IntervalTask(
        interval=SECONDS,
        function=samples.set_event_1,
        name="e2e_task",
        lock_at_most_for=SECONDS * 5,
        backend=backend,
        worker="worker_1",
    )
    task_2 = IntervalTask(
        interval=SECONDS,
        function=samples.set_event_2,
        name="e2e_task",
        lock_at_most_for=SECONDS * 5,
        backend=backend,
        worker="worker_2",
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(task_1)
        await tg.start(task_2)
        await samples.e2e_event_1.wait()
        await sleep(SECONDS * 2)
        tg.cancel_scope.cancel()

    # Assert - worker_1 acquired the lock, worker_2 was blocked
    assert samples.e2e_event_1.is_set()
    assert not samples.e2e_event_2.is_set()


async def test_interval_task_lock_at_least_for(backend: SyncBackend) -> None:
    """Test lock_at_least_for prevents re-execution on another worker."""
    # Arrange
    task_1 = IntervalTask(
        interval=0.5,
        function=samples.set_event_1,
        name="e2e_task",
        lock_at_most_for=10,
        backend=backend,
        worker="worker_1",
    )
    task_2 = IntervalTask(
        interval=0.5,
        function=samples.set_event_2,
        name="e2e_task",
        lock_at_most_for=10,
        backend=backend,
        worker="worker_2",
    )

    # Act - worker 1 executes then is cancelled, lock stays held for lock_at_least_for
    async with create_task_group() as tg:
        async with create_task_group() as tg_worker_1:
            await tg_worker_1.start(task_1)
            await samples.e2e_event_1.wait()
            tg_worker_1.cancel_scope.cancel()

        await tg.start(task_2)
        await sleep(0.2)
        worker_2_blocked = not samples.e2e_event_2.is_set()
        await sleep(0.5)
        worker_2_ran = samples.e2e_event_2.is_set()
        tg.cancel_scope.cancel()

    # Assert
    assert worker_2_blocked
    assert worker_2_ran


async def test_interval_task_lock_at_most_for(backend: SyncBackend) -> None:
    """Test lock_at_most_for auto-expires when task takes too long."""
    # Arrange
    task_1 = IntervalTask(
        interval=SECONDS,
        function=samples.worker_1_hold,
        name="e2e_task",
        lock_at_most_for=0.2,
        backend=backend,
        worker="worker_1",
    )
    task_2 = IntervalTask(
        interval=SECONDS,
        function=samples.set_event_2,
        name="e2e_task",
        lock_at_most_for=0.2,
        backend=backend,
        worker="worker_2",
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(task_1)
        await samples.e2e_event_1.wait()
        await tg.start(task_2)
        await sleep(0.05)
        worker_2_blocked = not samples.e2e_event_2.is_set()
        await sleep(0.3)
        worker_2_ran = samples.e2e_event_2.is_set()
        tg.cancel_scope.cancel()

    # Assert
    assert worker_2_blocked
    assert worker_2_ran


async def test_interval_task_with_lock_would_block_debug_log(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test WouldBlock from lock logs at DEBUG."""
    # Arrange
    caplog.set_level("DEBUG")
    task_1 = IntervalTask(
        interval=SECONDS,
        function=samples.worker_1_hold,
        name="e2e_task",
        lock_at_most_for=SECONDS * 5,
        backend=backend,
        worker="worker_1",
    )
    task_2 = IntervalTask(
        interval=SECONDS,
        function=samples.noop,
        name="e2e_task",
        lock_at_most_for=SECONDS * 5,
        backend=backend,
        worker="worker_2",
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(task_1)
        await samples.e2e_event_1.wait()
        await tg.start(task_2)
        await sleep(SECONDS * 2)
        tg.cancel_scope.cancel()

    # Assert
    assert any(
        "Task skipped (already locked):" in record.message
        for record in caplog.records
        if record.levelname == "DEBUG"
    )
    assert not any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_with_lock_sequential_executions(
    backend: SyncBackend,
) -> None:
    """Test same worker executes again after lock_at_least_for expires."""
    # Arrange
    task = IntervalTask(
        interval=SECONDS,
        function=samples.set_event_1,
        name="e2e_task",
        lock_at_most_for=SECONDS * 5,
        backend=backend,
        worker="worker_1",
    )

    # Act - wait for first execution, reset event, wait for second
    async with create_task_group() as tg:
        await tg.start(task)
        await samples.e2e_event_1.wait()
        samples.e2e_event_1 = Event()
        await samples.e2e_event_1.wait()
        tg.cancel_scope.cancel()


async def test_interval_task_with_leader_executes(
    backend: SyncBackend,
) -> None:
    """Test IntervalTask executes when worker is leader."""
    # Arrange
    leader = LeaderElection("test-leader", backend=backend, worker="worker_1")
    task = IntervalTask(
        interval=SECONDS,
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        leader=leader,
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(leader)
        await tg.start(task)
        await samples.e2e_event_1.wait()
        tg.cancel_scope.cancel()


async def test_interval_task_with_leader_skips_when_not_leader(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test IntervalTask skips when worker is not leader."""
    # Arrange
    caplog.set_level("DEBUG")
    leader_1 = LeaderElection("test-leader", backend=backend, worker="worker_1")
    leader_2 = LeaderElection("test-leader", backend=backend, worker="worker_2")
    task = IntervalTask(
        interval=SECONDS,
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        leader=leader_2,
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(leader_1)
        await tg.start(leader_2)
        await tg.start(task)
        await sleep(SECONDS * 3)
        tg.cancel_scope.cancel()

    # Assert
    assert not samples.e2e_event_1.is_set()
    assert any(
        "Task skipped (already locked):" in record.message
        for record in caplog.records
        if record.levelname == "DEBUG"
    )
