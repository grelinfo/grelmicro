"""Task Router."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from typing_extensions import Doc

from grelmicro.task.errors import TaskAddOperationError

if TYPE_CHECKING:
    from grelmicro.coordination.abc import (
        LockBackend,
        LockPrimitive,
        ScheduleBackend,
    )
    from grelmicro.coordination.leaderelection import LeaderElection
    from grelmicro.task.abc import Task


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

    def interval(
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
                Also used as the lock name when distributed locking is enabled.
                """,
            ),
        ] = None,
        max_lock_seconds: Annotated[
            float | None,
            Doc(
                """
                The maximum duration in seconds to hold the lock (crash protection).

                Setting this enables distributed locking: the task runs at most once
                per interval across all workers. Must be >= ``seconds``.
                When ``leader`` is set without this, defaults to ``seconds * 5``.
                """,
            ),
        ] = None,
        min_lock_seconds: Annotated[
            float | None,
            Doc(
                """
                The minimum duration in seconds to hold the lock after task completion.

                Prevents re-execution on other nodes before this duration has elapsed.
                Defaults to ``seconds`` when distributed locking is enabled.
                Requires ``max_lock_seconds`` or ``leader`` to be set.
                """,
            ),
        ] = None,
        leader: Annotated[
            "LeaderElection | None",
            Doc(
                """
                Optional leader election for leader gating.

                When provided, the task only executes on the leader worker.
                Implies distributed locking (lock is automatically configured).
                """,
            ),
        ] = None,
        backend: Annotated[
            "LockBackend | None",
            Doc(
                """
                The distributed lock backend.

                By default, resolves through the active `Grelmicro` app's
                `Coordination` component. Only used when distributed locking
                is enabled.
                """,
            ),
        ] = None,
        worker: Annotated[
            str | UUID | None,
            Doc(
                """
                The worker identity.

                By default, a UUIDv1 will be generated.
                Only used when distributed locking is enabled.
                """,
            ),
        ] = None,
        sync: Annotated[
            "LockPrimitive | None",
            Doc(
                """
                Optional resource-level synchronization primitive.

                Layered on top of any distributed scheduling chosen via
                ``max_lock_seconds`` or ``leader``. Use a ``Lock`` to serialise
                execution against a shared resource. Whether the task runs on
                every worker or only one is governed by ``max_lock_seconds``
                and ``leader``, not this parameter.
                """,
            ),
        ] = None,
    ) -> Callable[
        [Callable[..., Any | Awaitable[Any]]],
        Callable[..., Any | Awaitable[Any]],
    ]:
        """Decorate function to add it as an interval task.

        Supports three modes:

        - **Local**: No lock params, runs on every worker, every interval.
        - **Distributed lock**: Set ``max_lock_seconds`` to run at most once per
          interval across all workers.
        - **Leader-gated**: Set ``leader`` to restrict execution to the leader
          worker (lock is implied).

        Raises:
            FunctionTypeError: If the task name generation fails.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If max_lock_seconds is less than seconds.
            ValueError: If min_lock_seconds is set without max_lock_seconds or leader.
            ValueError: If min_lock_seconds is greater than max_lock_seconds.
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
                    max_lock_seconds=max_lock_seconds,
                    min_lock_seconds=min_lock_seconds,
                    leader=leader,
                    backend=backend,
                    worker=worker,
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
