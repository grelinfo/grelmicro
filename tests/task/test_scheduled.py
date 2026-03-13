"""Test Scheduled Task."""

from collections.abc import AsyncGenerator

import pytest
from anyio import Event, create_task_group, sleep, sleep_forever
from pytest_mock import MockFixture

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.leaderelection import LeaderElection
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.task._scheduled import ScheduledTask
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


def test_scheduled_task_init() -> None:
    """Test Scheduled Task Initialization."""
    # Arrange
    backend = MemorySyncBackend()
    # Act
    task = ScheduledTask(seconds=1, function=test1, backend=backend)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_scheduled_task_init_with_name() -> None:
    """Test Scheduled Task Initialization with Name."""
    # Arrange
    backend = MemorySyncBackend()
    # Act
    task = ScheduledTask(
        seconds=1, function=test1, name="my-task", backend=backend
    )
    # Assert
    assert task.name == "my-task"


def test_scheduled_task_init_invalid_seconds() -> None:
    """Test Scheduled Task Initialization with Invalid Seconds."""
    # Arrange
    backend = MemorySyncBackend()
    # Act / Assert
    with pytest.raises(ValueError, match="seconds must be greater than 0"):
        ScheduledTask(seconds=0, function=test1, backend=backend)


def test_scheduled_task_init_lock_at_most_for_default() -> None:
    """Test Scheduled Task Initialization lock_at_most_for Default."""
    # Arrange
    backend = MemorySyncBackend()
    # Act
    task = ScheduledTask(seconds=10, function=test1, backend=backend)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_scheduled_task_init_lock_at_most_for_custom() -> None:
    """Test Scheduled Task Initialization lock_at_most_for Custom."""
    # Arrange
    backend = MemorySyncBackend()
    # Act
    task = ScheduledTask(
        seconds=10, function=test1, lock_at_most_for=100, backend=backend
    )
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_scheduled_task_init_lock_at_most_for_validation() -> None:
    """Test Scheduled Task Initialization lock_at_most_for Validation."""
    # Arrange
    backend = MemorySyncBackend()
    # Act / Assert
    with pytest.raises(
        ValueError,
        match="lock_at_most_for must be greater than or equal to seconds",
    ):
        ScheduledTask(
            seconds=10, function=test1, lock_at_most_for=5, backend=backend
        )


async def test_scheduled_task_start(backend: SyncBackend) -> None:
    """Test Scheduled Task Start."""
    # Arrange
    task = ScheduledTask(seconds=SECONDS, function=notify, backend=backend)
    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        async with condition:
            await condition.wait()
        tg.cancel_scope.cancel()


async def test_scheduled_task_execution_error(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Scheduled Task Execution Error."""
    # Arrange
    task = ScheduledTask(seconds=SECONDS, function=always_fail, backend=backend)
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


async def test_scheduled_task_synchronization_error(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
    mocker: MockFixture,
) -> None:
    """Test Scheduled Task Synchronization Error."""
    # Arrange
    task = ScheduledTask(seconds=SECONDS, function=notify, backend=backend)
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


async def test_scheduled_task_stop(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
    mocker: MockFixture,
) -> None:
    """Test Scheduled Task Stop."""
    # Arrange
    caplog.set_level("INFO")

    class CustomBaseException(BaseException):
        pass

    mocker.patch(
        "grelmicro.task._scheduled.sleep", side_effect=CustomBaseException
    )
    task = ScheduledTask(seconds=1, function=test1, backend=backend)

    async def scheduled_task_during_runtime_error() -> None:
        async with create_task_group() as tg:
            await tg.start(task)
            await sleep_forever()

    # Act
    with pytest.raises(BaseExceptionGroup):
        await scheduled_task_during_runtime_error()

    # Assert
    assert any(
        "Task stopped:" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )


# --- End-to-end: ScheduledTask ---


@pytest.fixture(autouse=True)
def _reset_e2e_state() -> None:
    """Reset shared e2e state before each test."""
    samples.e2e_event_1 = Event()
    samples.e2e_event_2 = Event()
    samples.e2e_counter = {"worker_1": 0, "worker_2": 0}


async def test_scheduled_task_two_workers(backend: SyncBackend) -> None:
    """Test only one worker executes when both use ScheduledTask."""
    # Arrange
    task_1 = ScheduledTask(
        seconds=SECONDS,
        function=samples.worker_1_count,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
    )
    task_2 = ScheduledTask(
        seconds=SECONDS,
        function=samples.worker_2_count,
        name="e2e_task",
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

    # Assert
    assert samples.e2e_counter["worker_1"] >= 1
    assert samples.e2e_counter["worker_2"] == 0


async def test_scheduled_task_lock_at_least_for(backend: SyncBackend) -> None:
    """Test lock_at_least_for prevents re-execution on another worker."""
    # Arrange
    task_1 = ScheduledTask(
        seconds=0.5,
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        lock_at_most_for=10,
    )
    task_2 = ScheduledTask(
        seconds=0.5,
        function=samples.set_event_2,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        lock_at_most_for=10,
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


async def test_scheduled_task_lock_at_most_for(backend: SyncBackend) -> None:
    """Test lock_at_most_for auto-expires when task takes too long."""
    # Arrange
    task_1 = ScheduledTask(
        seconds=SECONDS,
        function=samples.worker_1_hold,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        lock_at_most_for=0.2,
    )
    task_2 = ScheduledTask(
        seconds=SECONDS,
        function=samples.set_event_2,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        lock_at_most_for=0.2,
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


async def test_scheduled_task_would_block_debug_log(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test WouldBlock from TaskLock logs at DEBUG in ScheduledTask."""
    # Arrange
    caplog.set_level("DEBUG")
    task_1 = ScheduledTask(
        seconds=SECONDS,
        function=samples.worker_1_hold,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
    )
    task_2 = ScheduledTask(
        seconds=SECONDS,
        function=samples.noop,
        name="e2e_task",
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


async def test_scheduled_task_sequential_executions(
    backend: SyncBackend,
) -> None:
    """Test same worker executes again after lock_at_least_for expires."""
    # Arrange
    task = ScheduledTask(
        seconds=SECONDS,
        function=samples.set_event_1,
        name="e2e_task",
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


async def test_scheduled_task_with_leader_executes(
    backend: SyncBackend,
) -> None:
    """Test Scheduled Task executes when worker is leader."""
    # Arrange
    leader = LeaderElection("test-leader", backend=backend, worker="worker_1")
    task = ScheduledTask(
        seconds=SECONDS,
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


async def test_scheduled_task_with_leader_skips_when_not_leader(
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Scheduled Task skips when worker is not leader."""
    # Arrange
    caplog.set_level("DEBUG")
    leader_1 = LeaderElection("test-leader", backend=backend, worker="worker_1")
    leader_2 = LeaderElection("test-leader", backend=backend, worker="worker_2")
    task = ScheduledTask(
        seconds=SECONDS,
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
