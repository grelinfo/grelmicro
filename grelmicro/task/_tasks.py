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
        shutdown_timeout: Annotated[
            float,
            Doc(
                """
                Seconds to let running tasks finish their current unit of
                work on shutdown before they are force-cancelled. On exit
                a stop signal is raised so tasks unwind as soon as their
                in-flight work completes; this only bounds how long a task
                stuck mid-work delays shutdown.

                Defaults to `30.0`, matching Kubernetes'
                `terminationGracePeriodSeconds`. Keep it at or below the
                pod grace period so draining finishes before `SIGKILL`.
                Set to `0` to cancel immediately without draining.
                """,
            ),
        ] = 30.0,
    ) -> None:
        """Initialize Tasks.

        Raises:
            ValueError: If `shutdown_timeout` is negative.
        """
        TaskRouter.__init__(self, tasks=tasks)

        if shutdown_timeout < 0:
            msg = "shutdown_timeout must be greater than or equal to 0"
            raise ValueError(msg)

        self._auto_start = auto_start
        self._shutdown_timeout = shutdown_timeout
        self._task_group: asyncio.TaskGroup | None = None
        self._task_handles: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    async def __aenter__(self) -> Self:
        """Enter the context manager."""
        self._exit_stack = AsyncExitStack()
        self._stop = asyncio.Event()
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
        """Exit the context manager, draining tasks before forcing cancel."""
        if not self._task_group or not self._exit_stack:
            raise OutOfContextError(self, "__aexit__")
        await self._drain()
        return await self._exit_stack.__aexit__(exc_type, exc_value, traceback)

    async def _drain(self) -> None:
        """Stop tasks gracefully, force-cancelling stragglers after the timeout.

        Sets the shared stop signal so each task breaks once its current
        iteration finishes, then waits up to `shutdown_timeout`. Tasks
        still running at the deadline are cancelled.
        """
        self._stop.set()
        handles = self._task_handles
        if handles and self._shutdown_timeout > 0:
            _, pending = await asyncio.wait(
                handles, timeout=self._shutdown_timeout
            )
        else:
            pending = set(handles)
        for handle in pending:
            handle.cancel()
        self._task_handles.clear()

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
                task(ready=ready, stop=self._stop), name=task.name
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
