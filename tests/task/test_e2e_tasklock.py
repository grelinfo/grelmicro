"""End-to-end tests for IntervalTask with TaskLock.

These tests are parametrized over both the deprecated (sync=TaskLock()) and
new (max_lock_seconds=/backend=) APIs to avoid duplication.
"""

import pytest
from anyio import Event, create_task_group, sleep

from grelmicro.sync.abc import SyncBackend
from tests.task import samples
from tests.task.conftest import TaskFactory

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]

INTERVAL = 0.1


async def test_tasklock_basic_execution(
    backend: SyncBackend, task_factory: TaskFactory
) -> None:
    """Test IntervalTask executes with TaskLock."""
    task = task_factory(
        seconds=INTERVAL,
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_lock_seconds=0.001,
        max_lock_seconds=10,
    )

    async with create_task_group() as tg:
        await tg.start(task)
        await samples.e2e_event_1.wait()
        tg.cancel_scope.cancel()


async def test_tasklock_two_workers(
    backend: SyncBackend, task_factory: TaskFactory
) -> None:
    """Test only one worker executes when both use TaskLock on the same resource."""
    task_1 = task_factory(
        seconds=INTERVAL,
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    task_2 = task_factory(
        seconds=INTERVAL,
        function=samples.set_event_2,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        min_lock_seconds=1,
        max_lock_seconds=10,
    )

    async with create_task_group() as tg:
        await tg.start(task_1)
        await tg.start(task_2)
        await samples.e2e_event_1.wait()
        await sleep(INTERVAL * 2)
        tg.cancel_scope.cancel()

    assert samples.e2e_event_1.is_set()
    assert not samples.e2e_event_2.is_set()


async def test_tasklock_min_lock_seconds(
    backend: SyncBackend, task_factory: TaskFactory
) -> None:
    """Test min_lock_seconds prevents re-execution on another worker."""
    min_lock = 0.5
    task_1 = task_factory(
        seconds=INTERVAL,
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_lock_seconds=min_lock,
        max_lock_seconds=10,
    )
    task_2 = task_factory(
        seconds=INTERVAL,
        function=samples.set_event_2,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        min_lock_seconds=min_lock,
        max_lock_seconds=10,
    )

    async with create_task_group() as tg:
        async with create_task_group() as tg_worker_1:
            await tg_worker_1.start(task_1)
            await samples.e2e_event_1.wait()
            tg_worker_1.cancel_scope.cancel()

        await tg.start(task_2)
        await sleep(min_lock * 0.4)
        worker_2_blocked = not samples.e2e_event_2.is_set()
        await sleep(min_lock * 1.5)
        worker_2_ran = samples.e2e_event_2.is_set()
        tg.cancel_scope.cancel()

    assert worker_2_blocked
    assert worker_2_ran


async def test_tasklock_max_lock_seconds(
    backend: SyncBackend, task_factory: TaskFactory
) -> None:
    """Test max_lock_seconds auto-expires when task takes too long."""
    max_lock = 0.2
    task_1 = task_factory(
        seconds=INTERVAL,
        function=samples.worker_1_hold,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_lock_seconds=0.01,
        max_lock_seconds=max_lock,
    )
    task_2 = task_factory(
        seconds=INTERVAL,
        function=samples.set_event_2,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        min_lock_seconds=0.01,
        max_lock_seconds=max_lock,
    )

    async with create_task_group() as tg:
        await tg.start(task_1)
        await samples.e2e_event_1.wait()
        await tg.start(task_2)
        await sleep(max_lock * 0.25)
        worker_2_blocked = not samples.e2e_event_2.is_set()
        await sleep(max_lock * 1.5)
        worker_2_ran = samples.e2e_event_2.is_set()
        tg.cancel_scope.cancel()

    assert worker_2_blocked
    assert worker_2_ran


async def test_tasklock_would_block_debug_log(
    backend: SyncBackend,
    task_factory: TaskFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test WouldBlock from TaskLock logs at DEBUG, not ERROR."""
    caplog.set_level("DEBUG")
    task_1 = task_factory(
        seconds=INTERVAL,
        function=samples.worker_1_hold,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_lock_seconds=1,
        max_lock_seconds=10,
    )
    task_2 = task_factory(
        seconds=INTERVAL,
        function=samples.noop,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        min_lock_seconds=1,
        max_lock_seconds=10,
    )

    async with create_task_group() as tg:
        await tg.start(task_1)
        await samples.e2e_event_1.wait()
        await tg.start(task_2)
        await sleep(INTERVAL * 2)
        tg.cancel_scope.cancel()

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


async def test_tasklock_same_worker_blocked_by_min_lock(
    backend: SyncBackend, task_factory: TaskFactory
) -> None:
    """Test same worker cannot re-acquire before min_lock_seconds expires.

    Bug: The deterministic token (worker:task:id) allowed the same worker to
    bypass min_lock_seconds because the backend treats same-token acquire as
    reentrant (current_token == token -> success).
    """
    min_lock = 1.0
    task = task_factory(
        seconds=0.1,
        function=samples.count_execution,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_lock_seconds=min_lock,
        max_lock_seconds=10,
    )

    async with create_task_group() as tg:
        await tg.start(task)
        await sleep(min_lock * 0.5)
        tg.cancel_scope.cancel()

    assert samples.execution_count == 1, (
        f"Expected 1 execution (min_lock_seconds={min_lock}s blocks re-acquire), "
        f"got {samples.execution_count}"
    )


async def test_tasklock_sequential_executions(
    backend: SyncBackend, task_factory: TaskFactory
) -> None:
    """Test same worker executes again after min_lock_seconds expires."""
    task = task_factory(
        seconds=INTERVAL,
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_lock_seconds=INTERVAL,
        max_lock_seconds=10,
    )

    async with create_task_group() as tg:
        await tg.start(task)
        await samples.e2e_event_1.wait()
        samples.e2e_event_1 = Event()
        await samples.e2e_event_1.wait()
        tg.cancel_scope.cancel()
