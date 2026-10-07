"""End-to-end tests for IntervalTask with TaskLock.

These tests build the task through a shared factory that wires a
``gate=TaskLock(...)`` onto the interval.
"""

import asyncio
from datetime import timedelta
from itertools import pairwise

import pytest

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination._protocol import LockBackend
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.task._interval import IntervalTask
from tests.task import samples
from tests.task._helpers import cancel_group, start_task
from tests.task.conftest import TaskFactory

pytestmark = [pytest.mark.timeout(10)]

INTERVAL = 0.1
WORKERS = 3


async def test_tasklock_basic_execution(
    backend: LockBackend, task_factory: TaskFactory
) -> None:
    """Test IntervalTask executes with TaskLock."""
    task = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_hold_duration=timedelta(seconds=INTERVAL),
        lease_duration=10,
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await samples.e2e_event_1.wait()
        cancel_group(tg)


async def test_tasklock_two_workers(
    backend: LockBackend, task_factory: TaskFactory
) -> None:
    """Test only one worker executes when both use TaskLock on the same resource."""
    task_1 = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_hold_duration=1,
        lease_duration=10,
    )
    task_2 = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.set_event_2,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        min_hold_duration=1,
        lease_duration=10,
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task_1)
        await start_task(tg, task_2)
        await samples.e2e_event_1.wait()
        await asyncio.sleep(INTERVAL * 2)
        cancel_group(tg)

    assert samples.e2e_event_1.is_set()
    assert not samples.e2e_event_2.is_set()


async def test_tasklock_min_hold_duration(
    backend: LockBackend, task_factory: TaskFactory
) -> None:
    """Test min_hold_duration prevents re-execution on another worker."""
    min_lock = 0.5
    task_1 = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_hold_duration=timedelta(seconds=min_lock),
        lease_duration=10,
    )
    task_2 = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.set_event_2,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        min_hold_duration=timedelta(seconds=min_lock),
        lease_duration=10,
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


async def test_tasklock_lease_renewed_while_the_body_runs(
    backend: LockBackend, task_factory: TaskFactory
) -> None:
    """A body running past lease_duration keeps its claim, so the peer waits."""
    max_lock = 0.2
    task_1 = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.worker_1_hold,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_hold_duration=timedelta(seconds=INTERVAL),
        lease_duration=timedelta(seconds=max_lock),
    )
    task_2 = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.set_event_2,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        min_hold_duration=timedelta(seconds=INTERVAL),
        lease_duration=timedelta(seconds=max_lock),
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task_1)
        await samples.e2e_event_1.wait()
        await start_task(tg, task_2)
        await asyncio.sleep(max_lock * 3)
        worker_2_ran = samples.e2e_event_2.is_set()
        cancel_group(tg)

    assert not worker_2_ran


async def test_tasklock_would_block_debug_log(
    backend: LockBackend,
    task_factory: TaskFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test WouldBlock from TaskLock logs at DEBUG, not ERROR."""
    caplog.set_level("DEBUG")
    task_1 = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.worker_1_hold,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_hold_duration=1,
        lease_duration=10,
    )
    task_2 = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.noop,
        name="e2e_task",
        backend=backend,
        worker="worker_2",
        min_hold_duration=1,
        lease_duration=10,
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
    backend: LockBackend, task_factory: TaskFactory
) -> None:
    """Test same worker cannot re-acquire before min_hold_duration expires.

    Bug: The deterministic token (worker:task:id) allowed the same worker to
    bypass min_hold_duration because the backend treats same-token acquire as
    reentrant (current_token == token -> success).
    """
    min_lock = 1.0
    task = task_factory(
        interval=timedelta(milliseconds=100),
        function=samples.count_execution,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_hold_duration=timedelta(seconds=min_lock),
        lease_duration=10,
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await asyncio.sleep(min_lock * 0.5)
        cancel_group(tg)

    assert samples.execution_count == 1, (
        f"Expected 1 execution (min_hold_duration={min_lock}s blocks re-acquire), "
        f"got {samples.execution_count}"
    )


async def test_tasklock_sequential_executions(
    backend: LockBackend, task_factory: TaskFactory
) -> None:
    """Test same worker executes again after min_hold_duration expires."""
    task = task_factory(
        interval=timedelta(seconds=INTERVAL),
        function=samples.set_event_1,
        name="e2e_task",
        backend=backend,
        worker="worker_1",
        min_hold_duration=timedelta(seconds=INTERVAL),
        lease_duration=10,
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await samples.e2e_event_1.wait()
        samples.e2e_event_1 = asyncio.Event()
        await samples.e2e_event_1.wait()
        cancel_group(tg)


async def test_claim_runs_each_interval_once_across_offset_workers(
    backend: LockBackend,
) -> None:
    """Workers whose timers are offset never both run the same interval.

    Each worker starts half an interval after the previous one, so a claim
    released when the body ends would let every worker win its own tick.
    """
    interval = 0.2
    workers = [
        IntervalTask(
            interval=timedelta(seconds=interval),
            function=samples.record_start,
            name="claimed",
            gate="claim",
        )
        for _ in range(WORKERS)
    ]
    async with (
        Grelmicro(uses=[Coordination(lock=backend)]),
        asyncio.TaskGroup() as tg,
    ):
        for worker in workers:
            await start_task(tg, worker)
            await asyncio.sleep(interval / 2)
        await asyncio.sleep(interval * 5)
        cancel_group(tg)

    starts = samples.run_starts
    assert len(starts) >= WORKERS
    assert min(b - a for a, b in pairwise(starts)) >= interval * 0.8


async def test_gate_lock_refreshes_from_the_body(
    backend: LockBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """The handle passed as the gate is the one the task holds, so it renews."""
    samples.gate_lock = TaskLock(
        backend=backend,
        lease_duration=10,
        min_hold_duration=timedelta(seconds=INTERVAL),
    )
    task = IntervalTask(
        interval=timedelta(seconds=INTERVAL),
        function=samples.refresh_gate_lock,
        gate=samples.gate_lock,
    )

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, task)
        await samples.e2e_event_1.wait()
        cancel_group(tg)

    assert not any(r.levelname == "ERROR" for r in caplog.records)


async def test_claim_renews_while_a_long_body_runs(
    backend: LockBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """A body longer than the lease keeps its claim, so no peer runs it too."""
    caplog.set_level("WARNING")
    samples.long_body_seconds = INTERVAL * 6
    workers = [
        IntervalTask(
            interval=timedelta(seconds=INTERVAL),
            function=samples.run_long_body,
            name="long",
            gate="claim",
        )
        for _ in range(2)
    ]
    async with (
        Grelmicro(uses=[Coordination(lock=backend)]),
        asyncio.TaskGroup() as tg,
    ):
        await start_task(tg, workers[0])
        await asyncio.sleep(INTERVAL / 2)
        await start_task(tg, workers[1])
        await samples.e2e_event_1.wait()
        cancel_group(tg)

    assert samples.execution_count == 1
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
