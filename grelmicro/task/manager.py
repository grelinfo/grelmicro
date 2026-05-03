"""Task Manager."""

import asyncio
from contextlib import AsyncExitStack
from logging import getLogger
from types import TracebackType
from typing import Annotated, Self

from typing_extensions import Doc

from grelmicro.errors import OutOfContextError
from grelmicro.task.abc import Task
from grelmicro.task.errors import TaskAddOperationError
from grelmicro.task.router import TaskRouter

logger = getLogger("grelmicro.task")


class TaskManager(TaskRouter):
    """Task Manager.

    `TaskManager` class, the main entrypoint to manage scheduled tasks.
    """

    def __init__(
        self,
        *,
        auto_start: Annotated[
            bool,
            Doc(
                """
                Automatically start all tasks.
                """,
            ),
        ] = True,
        tasks: Annotated[
            list[Task] | None,
            Doc(
                """
                A list of tasks to be started.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the task manager."""
        TaskRouter.__init__(self, tasks=tasks)

        self._auto_start = auto_start
        self._task_group: asyncio.TaskGroup | None = None

    async def __aenter__(self) -> Self:
        """Enter the context manager."""
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        self._task_group = await self._exit_stack.enter_async_context(
            asyncio.TaskGroup(),
        )
        if self._auto_start:
            await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the context manager."""
        if not self._task_group or not self._exit_stack:
            raise OutOfContextError(self, "__aexit__")
        # Cancel every still-running task in the group so the
        # AsyncExitStack can close cleanly.
        for child in list(self._task_group._tasks):  # noqa: SLF001
            child.cancel()
        return await self._exit_stack.__aexit__(exc_type, exc_value, traceback)

    async def start(self) -> None:
        """Start all tasks manually."""
        if not self._task_group:
            raise OutOfContextError(self, "start")

        if self._started:
            raise TaskAddOperationError

        self.do_mark_as_started()

        loop = asyncio.get_running_loop()
        for task in self.tasks:
            ready: asyncio.Future[None] = loop.create_future()
            self._task_group.create_task(task(ready=ready), name=task.name)
            await ready
        logger.debug("%s scheduled tasks started", len(self._tasks))
