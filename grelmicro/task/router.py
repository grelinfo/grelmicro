"""grelmicro Task Router."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from grelmicro.task.errors import TaskAddOperationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from grelmicro.sync.abc import SyncBackend, Synchronization
    from grelmicro.sync.leaderelection import LeaderElection
    from grelmicro.task.abc import Task


class TaskRouter:
    """Task Router.

    `TaskRouter` class, used to group task schedules, for example to structure an app in
    multiple files. It would then be included in the `TaskManager`, or in another
    `TaskRouter`.
    """

    def __init__(
        self,
        *,
        tasks: Annotated[
            list[Task] | None,
            Doc(
                """
                A list of tasks to be scheduled.
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

                .. deprecated::
                    The ``sync`` parameter is deprecated. Use the ``scheduled()``
                    decorator instead for distributed task execution with built-in
                    TaskLock.

                If None, no synchronization is used and the task will run on all workers.
                """,
            ),
        ] = None,
    ) -> Callable[
        [Callable[..., Any | Awaitable[Any]]],
        Callable[..., Any | Awaitable[Any]],
    ]:
        """Decorate function to add it as a local interval task.

        For distributed scheduled tasks with built-in locking, use ``scheduled()`` instead.

        Raises:
            FunctionTypeError: If the task name generation fails.
        """
        from grelmicro.task._interval import IntervalTask  # noqa: PLC0415

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

    def scheduled(
        self,
        *,
        seconds: Annotated[
            float,
            Doc(
                """
                The duration in seconds between each scheduling attempt.

                Each worker retries every N seconds, but only one worker executes
                per interval thanks to the built-in lock. Also used as the
                ``lock_at_least_for`` value.
                """,
            ),
        ],
        name: Annotated[
            str | None,
            Doc(
                """
                The name of the task.

                If None, a name will be generated automatically from the function.
                Also used as the TaskLock name.
                """,
            ),
        ] = None,
        lock_at_most_for: Annotated[
            float | None,
            Doc(
                """
                The maximum duration in seconds to hold the lock (crash protection).

                Defaults to ``seconds * 5``. Must be >= ``seconds``.
                """,
            ),
        ] = None,
        leader: Annotated[
            LeaderElection | None,
            Doc(
                """
                Optional leader election for leader gating.

                When provided, the task only executes on the leader worker.
                """,
            ),
        ] = None,
        backend: Annotated[
            SyncBackend | None,
            Doc(
                """
                The distributed lock backend.

                By default, uses the lock backend registry.
                """,
            ),
        ] = None,
        worker: Annotated[
            str | UUID | None,
            Doc(
                """
                The worker identity.

                By default, a UUIDv1 will be generated.
                """,
            ),
        ] = None,
    ) -> Callable[
        [Callable[..., Any | Awaitable[Any]]],
        Callable[..., Any | Awaitable[Any]],
    ]:
        """Decorate function to add it as a distributed scheduled task.

        The task runs at most once per interval across all workers, using a built-in
        TaskLock. Can optionally be gated behind a leader election.

        Raises:
            FunctionTypeError: If the task name generation fails.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If lock_at_most_for is less than seconds.
        """
        from grelmicro.task._scheduled import ScheduledTask  # noqa: PLC0415

        def decorator(
            function: Callable[[], None | Awaitable[None]],
        ) -> Callable[[], None | Awaitable[None]]:
            self.add_task(
                ScheduledTask(
                    function=function,
                    seconds=seconds,
                    name=name,
                    lock_at_most_for=lock_at_most_for,
                    leader=leader,
                    backend=backend,
                    worker=worker,
                ),
            )
            return function

        return decorator

    def include_router(self, router: TaskRouter) -> None:
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
