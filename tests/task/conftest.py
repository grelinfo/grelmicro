"""Shared fixtures for task tests."""

import asyncio
import warnings
from collections.abc import AsyncGenerator, Callable

import pytest

from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.tasklock import TaskLock
from grelmicro.task._interval import IntervalTask
from tests.task import samples

type TaskFactory = Callable[..., IntervalTask]

LOCK_NAME = "test_task_lock"


def _create_task_deprecated_api(
    *,
    seconds: float,
    function: Callable[..., object],
    name: str,
    backend: SyncBackend,
    worker: str,
    min_lock_seconds: float,
    max_lock_seconds: float,
) -> IntervalTask:
    """Create IntervalTask using deprecated sync=TaskLock() API."""
    task_lock = TaskLock(
        LOCK_NAME,
        backend=backend,
        worker=worker,
        min_lock_seconds=min_lock_seconds,
        max_lock_seconds=max_lock_seconds,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return IntervalTask(
            seconds=seconds,
            function=function,
            name=name,
            sync=task_lock,
        )


def _create_task_new_api(
    *,
    seconds: float,
    function: Callable[..., object],
    name: str,
    backend: SyncBackend,
    worker: str,
    min_lock_seconds: float,
    max_lock_seconds: float,
) -> IntervalTask:
    """Create IntervalTask using new max_lock_seconds/backend API."""
    return IntervalTask(
        seconds=seconds,
        function=function,
        name=name,
        max_lock_seconds=max_lock_seconds,
        min_lock_seconds=min_lock_seconds,
        backend=backend,
        worker=worker,
    )


@pytest.fixture(params=["deprecated", "new"], ids=["deprecated-api", "new-api"])
def task_factory(request: pytest.FixtureRequest) -> TaskFactory:
    """Return a factory that creates IntervalTask with either API style."""
    if request.param == "deprecated":
        return _create_task_deprecated_api
    return _create_task_new_api


@pytest.fixture
async def backend() -> AsyncGenerator[SyncBackend]:
    """Return Memory Synchronization Backend."""
    async with MemorySyncBackend() as backend:
        yield backend


@pytest.fixture(autouse=True)
def _reset_e2e_state() -> None:
    """Reset shared e2e state before each test."""
    samples.e2e_event_1 = asyncio.Event()
    samples.e2e_event_2 = asyncio.Event()
    samples.condition = asyncio.Condition()
    samples.e2e_counter = {"worker_1": 0, "worker_2": 0}
    samples.execution_count = 0
