"""Interval Task."""

import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    nullcontext,
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

_SyncFactory = Callable[[], AbstractAsyncContextManager[None]]


class IntervalTask(Task):
    """Interval Task.

    Use the `TaskManager.interval()` or `TaskRouter.interval()` decorator instead
    of creating IntervalTask objects directly.

    Supports three modes:
    - Local: No lock params, runs on every worker.
    - Distributed lock: Set ``lock_at_most_for`` to enable at-most-once per interval.
    - Leader-gated: Set ``leader`` to restrict execution to the leader worker.
    """

    def __init__(
        self,
        *,
        function: Callable[..., Any],
        name: str | None = None,
        interval: float,
        lock_at_most_for: float | None = None,
        lock_at_least_for: float | None = None,
        leader: LeaderElection | None = None,
        backend: SyncBackend | None = None,
        worker: str | UUID | None = None,
        sync: Synchronization | None = None,
    ) -> None:
        """Initialize the IntervalTask.

        Raises:
            FunctionTypeError: If the function is not supported.
            ValueError: If interval is less than or equal to 0.
            ValueError: If lock_at_most_for is less than interval.
            ValueError: If lock_at_least_for is set without lock_at_most_for or leader.
        """
        if interval <= 0:
            msg = "Interval must be greater than 0"
            raise ValueError(msg)

        if sync is not None:
            warnings.warn(
                "The 'sync' parameter on interval() is deprecated. "
                "Use lock_at_most_for and leader parameters instead.",
                DeprecationWarning,
                stacklevel=2,
            )

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._interval = interval
        self._async_function = self._prepare_async_function(function)

        # Determine if distributed lock is needed
        distributed = lock_at_most_for is not None or leader is not None

        if lock_at_least_for is not None and not distributed:
            msg = (
                "lock_at_least_for requires lock_at_most_for or leader "
                "to be set"
            )
            raise ValueError(msg)

        if distributed:
            resolved_lock_at_most_for = (
                lock_at_most_for
                if lock_at_most_for is not None
                else interval * 5
            )
            resolved_lock_at_least_for = (
                lock_at_least_for
                if lock_at_least_for is not None
                else interval
            )

            if resolved_lock_at_most_for < interval:
                msg = "lock_at_most_for must be greater than or equal to interval"
                raise ValueError(msg)

            if resolved_lock_at_least_for > resolved_lock_at_most_for:
                msg = (
                    "lock_at_least_for must be less than or equal to "
                    "lock_at_most_for"
                )
                raise ValueError(msg)

            task_lock = TaskLock(
                self._name,
                backend=backend,
                worker=worker,
                lock_at_least_for=resolved_lock_at_least_for,
                lock_at_most_for=resolved_lock_at_most_for,
            )
            self._sync_factory: _SyncFactory | None = _build_sync(
                leader=leader, task_lock=task_lock
            )
        elif sync is not None:
            # Legacy sync parameter support
            self._sync_factory = None
            self._legacy_sync: Synchronization | AbstractAsyncContextManager[None] = sync
        else:
            self._sync_factory = None
            self._legacy_sync = nullcontext()

    @property
    def name(self) -> str:
        """Return the task name."""
        return self._name

    async def __call__(
        self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED
    ) -> None:
        """Run the repeated task loop."""
        logger.info(
            "Task started (interval: %ss): %s", self._interval, self.name
        )
        task_status.started()
        try:
            while True:
                try:
                    if self._sync_factory is not None:
                        async with self._sync_factory():
                            try:
                                await self._async_function()
                            except Exception:
                                logger.exception(
                                    "Task execution error: %s", self.name
                                )
                    else:
                        async with self._legacy_sync:
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
                await sleep(self._interval)
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
