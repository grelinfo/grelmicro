"""Tasks."""

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


class Tasks(TaskRouter):
    """Tasks.

    `Tasks` class, the main entrypoint to manage scheduled tasks.
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
        """Initialize Tasks."""
        TaskRouter.__init__(self, tasks=tasks)

        self._auto_start = auto_start
        self._task_group: asyncio.TaskGroup | None = None
        self._task_handles: list[asyncio.Task[None]] = []

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
        for handle in self._task_handles:
            handle.cancel()
        self._task_handles.clear()
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
            handle = self._task_group.create_task(
                task(ready=ready), name=task.name
            )
            self._task_handles.append(handle)
            # Wait for the task to signal readiness, but surface its
            # completion or failure too. A task that returns or raises
            # before resolving ``ready`` would otherwise deadlock startup.
            done, _ = await asyncio.wait(
                {handle, ready}, return_when=asyncio.FIRST_COMPLETED
            )
            if handle in done and not ready.done():
                # Propagate the task's exception, or signal that it
                # exited without ever becoming ready.
                handle.result()
                msg = f"Task {task.name!r} exited before signaling readiness"
                raise RuntimeError(msg)
        logger.debug("%s scheduled tasks started", len(self._tasks))
