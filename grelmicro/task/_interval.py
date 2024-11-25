"""Interval Task."""

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from functools import partial
from inspect import iscoroutinefunction
from logging import getLogger
from typing import Any

from anyio import TASK_STATUS_IGNORED, sleep, to_thread
from anyio.abc import TaskStatus
from fast_depends import inject

from grelmicro.sync.abc import Synchronization
from grelmicro.task._utils import validate_and_generate_reference
from grelmicro.task.abc import Task

logger = getLogger("grelmicro.task")


class IntervalTask(Task):
    """Interval Task.

    Use the `TaskManager.interval()` or `SchedulerRouter.interval()` decorator instead
    of creating IntervalTask objects directly.
    """

    def __init__(
        self,
        *,
        function: Callable[..., Any],
        name: str | None = None,
        interval: float,
        sync: Synchronization | None = None,
    ) -> None:
        """Initialize the IntervalTask.

        Raises:
            FunctionNotSupportedError: If the function is not supported.
            ValueError: If internal is less than or equal to 0.
        """
        if interval <= 0:
            msg = "Interval must be greater than 0"
            raise ValueError(msg)

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._interval = interval
        self._async_function = self._prepare_async_function(function)
        self._sync = sync if sync else nullcontext()

    @property
    def name(self) -> str:
        """Return the lock name."""
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
                    async with self._sync:
                        try:
                            await self._async_function()
                        except Exception:
                            logger.exception(
                                "Task execution error: %s", self.name
                            )
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
