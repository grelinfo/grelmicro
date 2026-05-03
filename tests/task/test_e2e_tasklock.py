"""End-to-end tests for IntervalTask with TaskLock.

These tests are parametrized over both the deprecated (sync=TaskLock()) and
new (max_lock_seconds=/backend=) APIs to avoid duplication.
"""

import asyncio

import pytest

from grelmicro.sync.abc import SyncBackend
from tests.task import samples
from tests.task._helpers import cancel_group, start_task
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

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await samples.e2e_event_1.wait()
        cancel_group(tg)


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

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task_1)
        await start_task(tg, task_2)
        await samples.e2e_event_1.wait()
        await asyncio.sleep(INTERVAL * 2)
        cancel_group(tg)

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

    async with asyncio.TaskGroup() as tg:
        async with asyncio.TaskGroup() as tg_worker_1:
            await start_task(tg_worker_1, task_1)
            await samples.e2e_event_1.wait()
            cancel_group(tg_worker_1)

        await start_task(tg, task_2)
        await asyncio.sleep(min_lock * 0.4)
        worker_2_blocked = not samples.e2e_event_2.is_set()
        await asyncio.sleep(min_lock * 1.5)
        worker_2_ran = samples.e2e_event_2.is_set()
        cancel_group(tg)

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

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task_1)
        await samples.e2e_event_1.wait()
        await start_task(tg, task_2)
        await asyncio.sleep(max_lock * 0.25)
        worker_2_blocked = not samples.e2e_event_2.is_set()
        await asyncio.sleep(max_lock * 1.5)
        worker_2_ran = samples.e2e_event_2.is_set()
        cancel_group(tg)

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

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task_1)
        await samples.e2e_event_1.wait()
        await start_task(tg, task_2)
        await asyncio.sleep(INTERVAL * 2)
        cancel_group(tg)

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

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await asyncio.sleep(min_lock * 0.5)
        cancel_group(tg)

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

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await samples.e2e_event_1.wait()
        samples.e2e_event_1 = asyncio.Event()
        await samples.e2e_event_1.wait()
        cancel_group(tg)
