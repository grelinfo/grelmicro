"""Grelmicro Task Router."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from typing_extensions import Doc

from grelmicro.sync.abc import Synchronization
from grelmicro.task.abc import Task
from grelmicro.task.errors import TaskAddOperationError


class TaskRouter:
    """Task Router.

    `TaskRouter` class, used to group task schedules, for example to structure an app in
    multiple files. It would then included in the `TaskManager`, or in another
    `TaskRouter`.
    """

    def __init__(
        self,
        *,
        tasks: Annotated[
            list[Task] | None,
            Doc(
                """
                A list of schedules or scheduled tasks to be scheduled.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the task router."""
        self._started = False
        self._tasks: list[Task] = tasks or []
        self._routers: list[TaskRouter] = []

    @property
    def tasks(self) -> list[Task]:
        """List of scheduled tasks."""
        return self._tasks + [
            task for router in self._routers for task in router.tasks
        ]

    def add_task(self, task: Task) -> None:
        """Add a task to the scheduler."""
        if self._started:
            raise TaskAddOperationError

        self._tasks.append(task)

    def interval(
        self,
        *,
        seconds: Annotated[
            float,
            Doc(
                """
                The duration in seconds between each task run.

                Accuracy is not guaranteed and may vary with system load. Consider the
                execution time of the task when setting the interval.
                """,
            ),
        ],
        name: Annotated[
            str | None,
            Doc(
                """
                The name of the task.

                If None, a name will be generated automatically from the function.
                """,
            ),
        ] = None,
        sync: Annotated[
            Synchronization | None,
            Doc(
                """
                The synchronization primitive to use for the task.

                You can use a `LeasedLock` or a `LeaderElection`, for example. If None,
                no synchronization is used and the task will run on all workers.
                """,
            ),
        ] = None,
    ) -> Callable[
        [Callable[..., Any | Awaitable[Any]]],
        Callable[..., Any | Awaitable[Any]],
    ]:
        """Decorate function to add it to the task scheduler.

        Raises:
            TaskNameGenerationError: If the task name generation fails.
        """
        from grelmicro.task._interval import IntervalTask

        def decorator(
            function: Callable[[], None | Awaitable[None]],
        ) -> Callable[[], None | Awaitable[None]]:
            self.add_task(
                IntervalTask(
                    name=name,
                    function=function,
                    interval=seconds,
                    sync=sync,
                ),
            )
            return function

        return decorator

    def include_router(self, router: "TaskRouter") -> None:
        """Include another router in this router."""
        if self._started:
            raise TaskAddOperationError

        self._routers.append(router)

    def started(self) -> bool:
        """Check if the task manager has started."""
        return self._started

    def do_mark_as_started(self) -> None:
        """Mark the task manager as started.

        Do not call this method directly. It is called by the task manager when the task
        manager is started.
        """
        self._started = True
        for router in self._routers:
            router.do_mark_as_started()
