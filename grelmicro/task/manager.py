"""Grelmicro Task Manager."""

from contextlib import AsyncExitStack
from logging import getLogger
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Self

from anyio import create_task_group
from typing_extensions import Doc

from grelmicro.abc.task import Task
from grelmicro.errors import OutOfContextError
from grelmicro.task.errors import TaskAlreadyStartedError
from grelmicro.task.router import TaskRouter

if TYPE_CHECKING:
    from anyio.abc import TaskGroup

logger = getLogger("grelmicro.task")


class TaskManager(TaskRouter):
    """Task Manager.

    `TaskManager` app class, the main entrypoint to manage scheduled tasks.
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
        self._task_group: TaskGroup | None = None

    async def __aenter__(self) -> Self:
        """Enter the context manager."""
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        self._task_group = await self._exit_stack.enter_async_context(
            create_task_group(),
        )
        if self._auto_start:
            await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit the context manager."""
        if not self._task_group or not self._exit_stack:
            raise OutOfContextError(self, "__aexit__")
        self._task_group.cancel_scope.cancel()
        return await self._exit_stack.__aexit__(exc_type, exc_val, exc_tb)

    async def start(self) -> None:
        """Start scheduler with all tasks manually."""
        if not self._task_group:
            raise OutOfContextError(self, "start")

        if self._started:
            raise TaskAlreadyStartedError

        self.do_mark_as_started()

        for task in self.tasks:
            await self._task_group.start(task.__call__)
        logger.debug("%s scheduled tasks started", len(self._tasks))
