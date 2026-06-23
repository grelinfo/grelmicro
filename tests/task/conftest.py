"""Shared fixtures for task tests."""

import asyncio
from collections.abc import AsyncGenerator, Callable

import pytest

from grelmicro.coordination._protocol import LeaderElectionBackend, LockBackend
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
)
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.task._interval import IntervalTask
from tests.task import samples

type TaskFactory = Callable[..., IntervalTask]


def _create_task(
    *,
    seconds: float,
    function: Callable[..., object],
    name: str,
    backend: LockBackend,
    worker: str,
    min_hold_duration: float,
    lease_duration: float,
) -> IntervalTask:
    """Create IntervalTask using the lock=TaskLock() API."""
    return IntervalTask(
        seconds=seconds,
        function=function,
        name=name,
        lock=TaskLock(
            backend=backend,
            worker=worker,
            min_hold_duration=min_hold_duration,
            lease_duration=lease_duration,
        ),
    )


@pytest.fixture
def task_factory() -> TaskFactory:
    """Return a factory that creates IntervalTask with a lock."""
    return _create_task


@pytest.fixture
async def backend() -> AsyncGenerator[LockBackend]:
    """Return Memory Synchronization Backend."""
    async with MemoryLockAdapter() as backend:
        yield backend


@pytest.fixture
async def leader_backend() -> AsyncGenerator[LeaderElectionBackend]:
    """Return Memory Leader Election Adapter."""
    async with MemoryLeaderElectionAdapter() as backend:
        yield backend


@pytest.fixture(autouse=True)
def _reset_e2e_state() -> None:
    """Reset shared e2e state before each test."""
    samples.e2e_event_1 = asyncio.Event()
    samples.e2e_event_2 = asyncio.Event()
    samples.condition = asyncio.Condition()
    samples.e2e_counter = {"worker_1": 0, "worker_2": 0}
    samples.execution_count = 0
