"""Interval Task."""

import warnings
from collections.abc import Awaitable, Callable
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
from grelmicro.sync.lock import Lock
from grelmicro.sync.tasklock import TaskLock
from grelmicro.task._utils import validate_and_generate_reference
from grelmicro.task.abc import Task

logger = getLogger("grelmicro.task")


class IntervalTask(Task):
    """Interval Task.

    Use the `TaskManager.interval()` or `TaskRouter.interval()` decorator instead
    of creating IntervalTask objects directly.

    Supports three modes:
    - Local: No lock params, runs on every worker.
    - Distributed lock: Set ``max_lock_seconds`` to enable at-most-once per interval.
    - Leader-gated: Set ``leader`` to restrict execution to the leader worker.
    """

    def __init__(
        self,
        *,
        function: Callable[..., Any],
        name: str | None = None,
        seconds: float,
        max_lock_seconds: float | None = None,
        min_lock_seconds: float | None = None,
        leader: LeaderElection | None = None,
        backend: SyncBackend | None = None,
        worker: str | UUID | None = None,
        sync: Synchronization | None = None,
    ) -> None:
        """Initialize the IntervalTask.

        Raises:
            FunctionTypeError: If the function is not supported.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If deprecated sync (non-Lock) is combined with
                max_lock_seconds, min_lock_seconds, or leader.
            ValueError: If max_lock_seconds is less than seconds.
            ValueError: If min_lock_seconds is set without max_lock_seconds or leader.
            ValueError: If min_lock_seconds is greater than max_lock_seconds.
        """
        if seconds <= 0:
            msg = "seconds must be greater than 0"
            raise ValueError(msg)

        # Validate sync parameter usage
        if sync is not None and not isinstance(sync, Lock):
            if (
                max_lock_seconds is not None
                or min_lock_seconds is not None
                or leader is not None
            ):
                msg = (
                    "Cannot combine deprecated 'sync' parameter with "
                    "max_lock_seconds, min_lock_seconds, or leader. "
                    "Use a Lock for resource synchronization instead."
                )
                raise ValueError(msg)
            warnings.warn(
                "The 'sync' parameter on interval() is deprecated "
                "for TaskLock and LeaderElection. "
                "Use max_lock_seconds and leader parameters instead.",
                DeprecationWarning,
                stacklevel=2,
            )

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._seconds = seconds
        self._async_function = self._prepare_async_function(function)

        # Determine if distributed lock is needed
        distributed = max_lock_seconds is not None or leader is not None

        if min_lock_seconds is not None and not distributed:
            msg = (
                "min_lock_seconds requires max_lock_seconds or leader to be set"
            )
            raise ValueError(msg)

        # Build the synchronization chain
        resource_lock: Synchronization | None = (
            sync if isinstance(sync, Lock) else None
        )
        legacy_sync: Synchronization | None = (
            sync if sync is not None and not isinstance(sync, Lock) else None
        )

        if distributed:
            resolved_max_lock_seconds = (
                max_lock_seconds
                if max_lock_seconds is not None
                else seconds * 5
            )
            resolved_min_lock_seconds = (
                min_lock_seconds if min_lock_seconds is not None else seconds
            )

            if resolved_max_lock_seconds < seconds:
                msg = (
                    "max_lock_seconds must be greater than or equal to seconds"
                )
                raise ValueError(msg)

            if resolved_min_lock_seconds > resolved_max_lock_seconds:
                msg = (
                    "min_lock_seconds must be less than or equal to "
                    "max_lock_seconds"
                )
                raise ValueError(msg)

            task_lock = TaskLock(
                self._name,
                backend=backend,
                worker=worker,
                min_lock_seconds=resolved_min_lock_seconds,
                max_lock_seconds=resolved_max_lock_seconds,
            )
            self._sync_primitives: list[Synchronization] = _build_sync_list(
                leader=leader,
                task_lock=task_lock,
                resource_lock=resource_lock,
            )
        elif legacy_sync is not None:
            # Legacy sync parameter support (deprecated non-Lock types)
            self._sync_primitives = [legacy_sync]
        elif resource_lock is not None:
            # Lock for resource synchronization (not deprecated)
            self._sync_primitives = [resource_lock]
        else:
            self._sync_primitives = []

    @property
    def name(self) -> str:
        """Return the task name."""
        return self._name

    async def __call__(
        self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED
    ) -> None:
        """Run the repeated task loop."""
        logger.info(
            "Task started (interval: %ss): %s", self._seconds, self.name
        )
        task_status.started()
        try:
            while True:
                try:
                    await self._run_with_sync(self._sync_primitives)
                except WouldBlock as exc:
                    logger.debug("Task skipped: %s (%s)", self.name, exc)
                except Exception:
                    logger.exception(
                        "Task synchronization error: %s", self.name
                    )
                await sleep(self._seconds)
        finally:
            logger.info("Task stopped: %s", self.name)

    async def _run_with_sync(
        self, primitives: list[Synchronization], index: int = 0
    ) -> None:
        """Enter sync primitives via recursive nesting, then run the task.

        Using explicit recursion avoids AsyncExitStack and @asynccontextmanager
        overhead on every iteration.
        """
        if index >= len(primitives):
            try:
                await self._async_function()
            except Exception:
                logger.exception("Task execution error: %s", self.name)
            return

        async with primitives[index]:
            await self._run_with_sync(primitives, index + 1)

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


def _build_sync_list(
    *,
    leader: LeaderElection | None,
    task_lock: TaskLock,
    resource_lock: Synchronization | None = None,
) -> list[Synchronization]:
    """Build an ordered list of sync primitives."""
    primitives: list[Synchronization] = []
    if resource_lock is not None:
        primitives.append(resource_lock)
    if leader is not None:
        primitives.append(leader.guard())
    primitives.append(task_lock)
    return primitives
