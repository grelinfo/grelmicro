"""Task Router."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from grelmicro.task.errors import TaskAddOperationError

if TYPE_CHECKING:
    from grelmicro.coordination._protocol import (
        LockPrimitive,
        ScheduleBackend,
    )
    from grelmicro.coordination.leaderelection import LeaderElection
    from grelmicro.coordination.tasklock import TaskLock
    from grelmicro.task._protocol import Task


class TaskRouter:
    """Task Router.

    `TaskRouter` class, used to group task schedules, for example to structure an app in
    multiple files. It would then be included in the `Tasks`, or in another
    `TaskRouter`.
    """

    def __init__(
        self,
        *,
        tasks: Annotated[
            list["Task"] | None,
            Doc(
                """
                A list of tasks to be scheduled.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the task router."""
        self._started = False
        self._tasks: list[Any] = tasks or []
        self._routers: list[TaskRouter] = []

    @property
    def tasks(self) -> list["Task"]:
        """List of scheduled tasks."""
        return self._tasks + [
            task for router in self._routers for task in router.tasks
        ]

    def add_task(self, task: "Task") -> None:
        """Add a task to the scheduler."""
        if self._started:
            raise TaskAddOperationError

        self._tasks.append(task)

    def every(
        self,
        *,
        seconds: Annotated[
            float | timedelta,
            Doc(
                """
                The duration between each task run.

                Accepts a number of seconds or a `timedelta`.

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
        lock: Annotated[
            "TaskLock | None",
            Doc(
                """
                Optional distributed lock for at-most-once scheduling.

                Pass a `TaskLock` to run the task at most once per interval
                across all workers. Its ``lease_duration`` must be >=
                ``seconds``. When the lock keeps its default ``"default"``
                name, the task name is used so it does not need to be
                repeated. The lock's ``lease_duration``, ``min_hold_duration``,
                ``backend`` and ``worker`` are authoritative.
                """,
            ),
        ] = None,
        leader: Annotated[
            "LeaderElection | None",
            Doc(
                """
                Optional leader election for leader gating.

                When provided, the task only executes on the leader worker.
                Implies distributed locking (a lock is automatically
                configured with interval-aware defaults when no ``lock`` is
                given).
                """,
            ),
        ] = None,
        sync: Annotated[
            "LockPrimitive | None",
            Doc(
                """
                Optional resource-level synchronization primitive.

                Layered on top of any distributed scheduling chosen via
                ``lock`` or ``leader``. Use a ``Lock`` to serialise execution
                against a shared resource. Whether the task runs on every
                worker or only one is governed by ``lock`` and ``leader``, not
                this parameter.
                """,
            ),
        ] = None,
    ) -> Callable[
        [Callable[..., Any | Awaitable[Any]]],
        Callable[..., Any | Awaitable[Any]],
    ]:
        """Decorate a function to run it on a fixed interval.

        Supports three modes:

        - **Local**: No ``lock`` or ``leader``, runs on every worker, every
          interval.
        - **Distributed lock**: Pass a ``lock`` to run at most once per
          interval across all workers.
        - **Leader-gated**: Set ``leader`` to restrict execution to the leader
          worker (a lock is implied).

        Raises:
            FunctionTypeError: If the task name generation fails.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If the lock lease_duration is less than seconds.
        """
        from grelmicro.task._interval import IntervalTask  # noqa: PLC0415

        def decorator(
            function: Callable[[], None | Awaitable[None]],
        ) -> Callable[[], None | Awaitable[None]]:
            self.add_task(
                IntervalTask(
                    name=name,
                    function=function,
                    seconds=seconds,
                    lock=lock,
                    leader=leader,
                    sync=sync,
                ),
            )
            return function

        return decorator

    def cron(
        self,
        expr: Annotated[
            str,
            Doc(
                """
                The 5-field cron expression: ``minute hour day-of-month month day-of-week``.

                Each field supports ``*``, ``*/step``, ``a-b``, ``a-b/step``,
                a comma list, and a bare integer. Day of week is 0-6 with
                0 = Sunday (7 also means Sunday). When both day-of-month and
                day-of-week are restricted, a day matches if it matches either.
                """,
            ),
        ],
        *,
        timezone: Annotated[
            str,
            Doc(
                """
                The IANA timezone name used to compute fire times.

                Defaults to ``"UTC"``. Resolved with ``zoneinfo.ZoneInfo``.
                """,
            ),
        ] = "UTC",
        name: Annotated[
            str | None,
            Doc(
                """
                The name of the task.

                If None, a name will be generated automatically from the function.
                Also used as the schedule name for the durable last-fire state.
                """,
            ),
        ] = None,
        misfire_grace_seconds: Annotated[
            float | None,
            Doc(
                """
                How late a missed fire may run when a worker comes back.

                A fire missed while every worker was down replays once on
                restart only when now is within this many seconds of the fire.
                Past the budget, the fire is dropped. ``None`` (default) sets
                no budget, so any missed fire replays once, however late.
                Only the most recent missed fire ever runs, never a backlog.
                """,
            ),
        ] = None,
        backend: Annotated[
            "ScheduleBackend | None",
            Doc(
                """
                The durable schedule backend.

                By default, resolves through the active `Grelmicro` app's
                `Coordination` component. When no backend is available, the
                task runs on every worker, every fire.
                """,
            ),
        ] = None,
        sync: Annotated[
            "LockPrimitive | None",
            Doc(
                """
                Optional resource-level synchronization primitive.

                Wraps the body once this worker wins the fire. Use a ``Lock`` to
                serialise execution against a shared resource. Whether the task
                runs on every worker or only one is governed by the schedule
                backend, not this parameter.
                """,
            ),
        ] = None,
    ) -> Callable[
        [Callable[..., Any | Awaitable[Any]]],
        Callable[..., Any | Awaitable[Any]],
    ]:
        """Decorate function to add it as a cron task.

        Runs the task whenever the wall-clock time matches the cron
        expression in the given timezone.

        Each fire is claimed against a durable last-fire state, so the task
        runs at most once across every worker per fire. A fire missed while
        every worker was down replays once on restart, bounded by
        ``misfire_grace_seconds``, and only the most recent missed fire runs.
        Without a backend, the task runs on every worker, every fire.

        The guarantee is at-most-once. A worker that claims a fire and then
        crashes mid-run does not retry it, because the last-fire state already
        advanced. Make the body idempotent, or wrap it with ``@retry``, when
        correctness depends on completion.

        Raises:
            FunctionTypeError: If the task name generation fails.
            CronError: If the cron expression is invalid.
        """
        from grelmicro.task._cron import CronTask  # noqa: PLC0415

        def decorator(
            function: Callable[[], None | Awaitable[None]],
        ) -> Callable[[], None | Awaitable[None]]:
            self.add_task(
                CronTask(
                    name=name,
                    function=function,
                    expr=expr,
                    timezone=timezone,
                    misfire_grace_seconds=misfire_grace_seconds,
                    backend=backend,
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
