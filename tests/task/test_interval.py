"""Test Interval Task."""

import warnings
from collections.abc import AsyncGenerator

import pytest
from anyio import Event, create_task_group, sleep, sleep_forever
from pytest_mock import MockFixture

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.tasklock import TaskLock
from grelmicro.task._interval import IntervalTask
from tests.task import samples
from tests.task.samples import (
    BadLock,
    WouldBlockLock,
    always_fail,
    condition,
    notify,
    test1,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.timeout(10),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

INTERVAL = 0.1
SLEEP = 0.01
LOCK_NAME = "test_task_lock"


@pytest.fixture
async def backend() -> AsyncGenerator[SyncBackend]:
    """Return Memory Synchronization Backend."""
    async with MemorySyncBackend() as backend:
        yield backend


def test_interval_task_init() -> None:
    """Test Interval Task Initialization."""
    # Act
    task = IntervalTask(interval=1, function=test1)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_init_with_name() -> None:
    """Test Interval Task Initialization with Name."""
    # Act
    task = IntervalTask(interval=1, function=test1, name="test1")
    # Assert
    assert task.name == "test1"


def test_interval_task_init_with_invalid_interval() -> None:
    """Test Interval Task Initialization with Invalid Interval."""
    # Act / Assert
    with pytest.raises(ValueError, match="Interval must be greater than 0"):
        IntervalTask(interval=0, function=test1)


def test_interval_task_sync_deprecation_warning() -> None:
    """Test that sync= emits DeprecationWarning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        IntervalTask(interval=1, function=test1, sync=WouldBlockLock())

    assert len(w) == 1
    assert issubclass(w[0].category, DeprecationWarning)
    assert "sync" in str(w[0].message)
    assert "scheduled()" in str(w[0].message)


async def test_interval_task_start() -> None:
    """Test Interval Task Start."""
    # Arrange
    task = IntervalTask(interval=1, function=notify)
    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        async with condition:
            await condition.wait()
        tg.cancel_scope.cancel()


async def test_interval_task_execution_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Execution Error."""
    # Arrange
    task = IntervalTask(interval=1, function=always_fail)
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


async def test_interval_task_would_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task WouldBlock logs at DEBUG, not ERROR."""
    # Arrange
    caplog.set_level("DEBUG")
    task = IntervalTask(interval=1, function=notify, sync=WouldBlockLock())

    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        await sleep(SLEEP)
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


async def test_interval_task_synchronization_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Synchronization Error."""
    # Arrange
    task = IntervalTask(interval=1, function=notify, sync=BadLock())

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


async def test_interval_stop(
    caplog: pytest.LogCaptureFixture, mocker: MockFixture
) -> None:
    """Test Interval Task stop."""
    # Arrange
    caplog.set_level("INFO")

    class CustomBaseException(BaseException):
        pass

    mocker.patch(
        "grelmicro.task._interval.sleep", side_effect=CustomBaseException
    )
    task = IntervalTask(interval=1, function=test1)

    async def leader_election_during_runtime_error() -> None:
        async with create_task_group() as tg:
            await tg.start(task)
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


# --- End-to-end: IntervalTask + TaskLock ---


@pytest.fixture(autouse=True)
def _reset_e2e_state() -> None:
    """Reset shared e2e state before each test."""
    samples.e2e_event_1 = Event()
    samples.e2e_event_2 = Event()
    samples.e2e_counter = {"worker_1": 0, "worker_2": 0}


async def test_interval_task_with_tasklock(backend: SyncBackend) -> None:
    """Test Interval Task executes with TaskLock."""
    # Arrange
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_1",
        lock_at_least_for=0.001,
        lock_at_most_for=10,
    )
    task = IntervalTask(
        interval=INTERVAL,
        function=samples.set_event_1,
        name="e2e_task",
        sync=task_lock,
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        await samples.e2e_event_1.wait()
        tg.cancel_scope.cancel()


async def test_interval_task_with_tasklock_two_workers(
    backend: SyncBackend,
) -> None:
    """Test only one worker executes when both use TaskLock on the same resource."""
    # Arrange
    lock_1 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_1",
        lock_at_least_for=1,
        lock_at_most_for=10,
    )
    lock_2 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_2",
        lock_at_least_for=1,
        lock_at_most_for=10,
    )
    task_1 = IntervalTask(
        interval=INTERVAL,
        function=samples.worker_1_count,
        name="e2e_worker_1",
        sync=lock_1,
    )
    task_2 = IntervalTask(
        interval=INTERVAL,
        function=samples.worker_2_count,
        name="e2e_worker_2",
        sync=lock_2,
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(task_1)
        await tg.start(task_2)
        await samples.e2e_event_1.wait()
        await sleep(INTERVAL * 2)
        tg.cancel_scope.cancel()

    # Assert
    assert samples.e2e_counter["worker_1"] >= 1
    assert samples.e2e_counter["worker_2"] == 0


async def test_interval_task_with_tasklock_lock_at_least_for(
    backend: SyncBackend,
) -> None:
    """Test lock_at_least_for prevents re-execution on another worker."""
    # Arrange
    lock_1 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_1",
        lock_at_least_for=0.5,
        lock_at_most_for=10,
    )
    lock_2 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_2",
        lock_at_least_for=0.5,
        lock_at_most_for=10,
    )
    task_1 = IntervalTask(
        interval=INTERVAL,
        function=samples.set_event_1,
        name="e2e_worker_1",
        sync=lock_1,
    )
    task_2 = IntervalTask(
        interval=INTERVAL,
        function=samples.set_event_2,
        name="e2e_worker_2",
        sync=lock_2,
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


async def test_interval_task_with_tasklock_lock_at_most_for(
    backend: SyncBackend,
) -> None:
    """Test lock_at_most_for auto-expires when task takes too long."""
    # Arrange
    lock_1 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_1",
        lock_at_least_for=0.01,
        lock_at_most_for=0.2,
    )
    lock_2 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_2",
        lock_at_least_for=0.01,
        lock_at_most_for=0.2,
    )
    task_1 = IntervalTask(
        interval=INTERVAL,
        function=samples.worker_1_hold,
        name="e2e_worker_1",
        sync=lock_1,
    )
    task_2 = IntervalTask(
        interval=INTERVAL,
        function=samples.set_event_2,
        name="e2e_worker_2",
        sync=lock_2,
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


async def test_interval_task_with_tasklock_would_block_debug_log(
    backend: SyncBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """Test WouldBlock from TaskLock logs at DEBUG in IntervalTask."""
    # Arrange
    caplog.set_level("DEBUG")
    lock_1 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_1",
        lock_at_least_for=1,
        lock_at_most_for=10,
    )
    lock_2 = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_2",
        lock_at_least_for=1,
        lock_at_most_for=10,
    )
    task_1 = IntervalTask(
        interval=INTERVAL,
        function=samples.worker_1_hold,
        name="e2e_worker_1",
        sync=lock_1,
    )
    task_2 = IntervalTask(
        interval=INTERVAL,
        function=samples.noop,
        name="e2e_worker_2",
        sync=lock_2,
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(task_1)
        await samples.e2e_event_1.wait()
        await tg.start(task_2)
        await sleep(INTERVAL * 2)
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


async def test_interval_task_with_tasklock_sequential_executions(
    backend: SyncBackend,
) -> None:
    """Test same worker executes again after lock_at_least_for expires."""
    # Arrange
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker="worker_1",
        lock_at_least_for=0.1,
        lock_at_most_for=10,
    )
    task = IntervalTask(
        interval=INTERVAL,
        function=samples.set_event_1,
        name="e2e_task",
        sync=task_lock,
    )

    # Act - wait for first execution, reset event, wait for second
    async with create_task_group() as tg:
        await tg.start(task)
        await samples.e2e_event_1.wait()
        samples.e2e_event_1 = Event()
        await samples.e2e_event_1.wait()
        tg.cancel_scope.cancel()
