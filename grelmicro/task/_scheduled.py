"""Scheduled Task.

.. deprecated::
    Use :class:`IntervalTask` with ``lock_at_most_for`` or ``leader`` instead.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from functools import partial
from inspect import iscoroutinefunction
from logging import getLogger
from typing import Any
from uuid import UUID

from anyio import TASK_STATUS_IGNORED, WouldBlock, sleep, to_thread
from anyio.abc import TaskStatus
from fast_depends import inject

from grelmicro.sync.abc import SyncBackend, Synchronization
from grelmicro.sync.leaderelection import LeaderElection
from grelmicro.sync.tasklock import TaskLock
from grelmicro.task._utils import validate_and_generate_reference
from grelmicro.task.abc import Task

logger = getLogger("grelmicro.task")


class ScheduledTask(Task):
    """Scheduled Task with built-in TaskLock.

    .. deprecated::
        Use :class:`IntervalTask` with ``lock_at_most_for`` or ``leader`` instead.
        See ``TaskManager.interval()`` or ``TaskRouter.interval()``.
    """

    def __init__(
        self,
        *,
        function: Callable[..., Any],
        seconds: float,
        name: str | None = None,
        lock_at_most_for: float | None = None,
        leader: LeaderElection | None = None,
        backend: SyncBackend | None = None,
        worker: str | UUID | None = None,
    ) -> None:
        """Initialize the ScheduledTask.

        Raises:
            FunctionTypeError: If the function is not supported.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If lock_at_most_for is less than seconds.
        """
        if seconds <= 0:
            msg = "seconds must be greater than 0"
            raise ValueError(msg)

        resolved_lock_at_most_for = (
            lock_at_most_for if lock_at_most_for is not None else seconds * 5
        )

        if resolved_lock_at_most_for < seconds:
            msg = "lock_at_most_for must be greater than or equal to seconds"
            raise ValueError(msg)

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._seconds = seconds
        self._async_function = self._prepare_async_function(function)

        task_lock = TaskLock(
            self._name,
            backend=backend,
            worker=worker,
            lock_at_least_for=seconds,
            lock_at_most_for=resolved_lock_at_most_for,
        )
        self._sync = _build_sync(leader=leader, task_lock=task_lock)

    @property
    def name(self) -> str:
        """Return the task name."""
        return self._name

    async def __call__(
        self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED
    ) -> None:
        """Run the scheduled task loop."""
        logger.info(
            "Task started (interval: %ss): %s", self._seconds, self.name
        )
        task_status.started()
        try:
            while True:
                try:
                    async with self._sync():
                        try:
                            await self._async_function()
                        except Exception:
                            logger.exception(
                                "Task execution error: %s", self.name
                            )
                except WouldBlock:
                    logger.debug("Task skipped (already locked): %s", self.name)
                except Exception:
                    logger.exception(
                        "Task synchronization error: %s", self.name
                    )
                await sleep(self._seconds)
        finally:
            logger.info("Task stopped: %s", self.name)

    def _prepare_async_function(
        self, function: Callable[..., Any]
    ) -> Callable[..., Awaitable[Any]]:
        """Prepare the function with lock and ensure async function."""
        function = inject(function)
        return (
            function
            if iscoroutinefunction(function)
            else partial(to_thread.run_sync, function)
        )


_SyncFactory = Callable[[], AbstractAsyncContextManager[None]]


def _build_sync(
    *,
    leader: LeaderElection | None,
    task_lock: TaskLock,
) -> _SyncFactory:
    """Build a sync context manager factory from leader and task lock."""
    if leader is None:
        return _single_sync(task_lock)
    guard = leader.guard()
    return _sync_chain(guard, task_lock)


def _single_sync(sync: Synchronization) -> _SyncFactory:
    """Wrap a single sync primitive as a factory."""

    @asynccontextmanager
    async def _wrapper() -> AsyncIterator[None]:
        async with sync:
            yield

    return _wrapper


def _sync_chain(
    guard: Synchronization, task_lock: Synchronization
) -> _SyncFactory:
    """Wrap leader guard and task lock as a nested factory."""

    @asynccontextmanager
    async def _wrapper() -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(guard)
            await stack.enter_async_context(task_lock)
            yield

    return _wrapper
