"""Task Router."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from typing_extensions import Doc

from grelmicro._markers import Registered, mark_registered
from grelmicro.task._utils import normalize_timezone
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
        timezone: Annotated[
            str | None,
            Doc(
                """
                The IANA timezone name every cron task in this router uses.

                A cron task that passes its own ``timezone=`` keeps it.
                When None (the default), the router takes the timezone of
                the `Tasks` or `TaskRouter` that includes it, and falls
                back to ``"UTC"`` when nothing sets one.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the task router.

        Raises:
            TimezoneError: If no timezone of that name can be loaded.
        """
        self._started = False
        self._tasks: list[Any] = []
        self._routers: list[TaskRouter] = []
        self._timezone = (
            normalize_timezone(timezone) if timezone is not None else None
        )
        for task in tasks or []:
            self._add_task(task)

    @property
    def tasks(self) -> list["Task"]:
        """List of scheduled tasks."""
        return self._tasks + [
            task for router in self._routers for task in router.tasks
        ]

    @property
    def timezone(self) -> str | None:
        """The timezone this router declares, or None when it declares none.

        A router reports only what it was given. The timezone its tasks
        end up using is resolved by the owning `Tasks` when it starts, and
        a router does not read the value it would inherit.
        """
        return self._timezone

    def add_task(self, task: "Task") -> None:
        """Add a task to the scheduler.

        Marks the function a task exposes as `function`, so a decorator
        applied below the one that registered it is refused instead of
        wrapping calls the schedule will never make. `IntervalTask` and
        `CronTask` both expose it. A `Task` of your own is marked when
        it does the same, and left alone when it does not.
        """
        self._add_task(task)

    def _add_task(self, task: "Task") -> None:
        """Add a task without going through the overridable entry point.

        The constructor adds this way, because a subclass that overrides
        `add_task` would otherwise run it against a half-built instance
        of its own.
        """
        if self._started:
            raise TaskAddOperationError

        try:
            function = getattr(task, "function", None)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001
            function = None
        if callable(function):
            mark_registered(function, Registered.TASK, self)
        self._tasks.append(task)

    def _resolve_timezones(self, inherited: str) -> None:
        """Give every cron task below this router its effective timezone.

        Walks the tree once, so the result does not depend on the order
        the routers were included or the tasks were added. A router that
        declares a timezone overrides the inherited one for its own
        subtree, and a task that declares one keeps it.
        """
        from grelmicro.task._cron import CronTask  # noqa: PLC0415

        effective = self._timezone or inherited
        for task in self._tasks:
            if isinstance(task, CronTask):
                task._set_default_timezone(effective)  # noqa: SLF001
        for router in self._routers:
            router._resolve_timezones(effective)  # noqa: SLF001

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
        gate: Annotated[
            'Literal["claim"] | TaskLock | LeaderElection | None',
            Doc(
                """
                Which workers run each interval.

                - `None` (the default): every worker runs every interval.
                - `"claim"`: one worker claims each interval and runs it. The
                  claim is held for the whole interval, with a lease of two
                  intervals.
                - A `TaskLock`: one worker claims each interval, with the
                  lock's own `lease_duration`, `backend` and `worker`. Its
                  `min_hold_duration` must be at least `seconds`, now and on
                  every later `reconfigure`. A lock still named `"default"`
                  takes the task name.
                - A `LeaderElection`: only the elected worker runs the task.
                  It claims each interval as with `"claim"`, so a leader
                  handover never runs one interval twice.

                A gated task renews its claim every third of the lease from
                the moment it holds it until the body ends, so a body, and a
                wait for a `sync` lock before it, may take as long as needed. A
                worker that crashes frees its claim within the lease. A
                claim lost anyway, to a backend outage or a stalled event
                loop, logs a warning and lets the body finish.
                """,
            ),
        ] = None,
        sync: Annotated[
            "LockPrimitive | None",
            Doc(
                """
                Optional resource-level synchronization primitive.

                Wraps the body once the gate lets this worker through. Use a
                `Lock` to serialise execution against a shared resource.
                Which workers run the task is set by `gate`, not this
                parameter.
                """,
            ),
        ] = None,
    ) -> Callable[
        [Callable[..., Any | Awaitable[Any]]],
        Callable[..., Any | Awaitable[Any]],
    ]:
        """Decorate a function to run it on a fixed interval.

        Every worker runs it by default. Pass `gate="claim"` to run it
        on one worker per interval, or a `LeaderElection` to run it on
        the elected worker.

        Raises:
            FunctionTypeError: If the task name generation fails.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If the gate `TaskLock` already gates another task.
            SettingsValidationError: If the gate `TaskLock` holds a claim
                for less than `seconds`.
            TypeError: If `gate` is not a supported value, or `sync` is a
                leader election.
        """
        from grelmicro.task._interval import IntervalTask  # noqa: PLC0415

        def decorator(
            function: Callable[[], Awaitable[None] | None],
        ) -> Callable[[], Awaitable[None] | None]:
            self.add_task(
                IntervalTask(
                    name=name,
                    function=function,
                    seconds=seconds,
                    gate=gate,
                    sync=sync,
                ),
            )
            # `add_task` marks the same function already. This covers a
            # subclass that overrides it and schedules another way.
            mark_registered(function, Registered.TASK, self)
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
            str | None,
            Doc(
                """
                The IANA timezone name used to compute fire times.

                When None (the default), the task takes the timezone
                configured on the `Tasks` or `TaskRouter` it belongs to,
                and falls back to ``"UTC"`` when nothing sets one. Pass a
                name here to pin one task to a different timezone than
                the rest.
                """,
            ),
        ] = None,
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

                Read only by a gated task. A fire missed while every worker was down replays once on
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

                Read only by a gated task. By default, resolves through the
                active `Grelmicro` app's `Coordination` component.
                """,
            ),
        ] = None,
        gate: Annotated[
            'Literal["claim"] | LeaderElection | None',
            Doc(
                """
                Which workers run each fire.

                - `None` (the default): every worker runs every fire.
                - `"claim"`: one worker claims each fire against the durable
                  schedule backend and runs it. A fire missed while every
                  worker was down replays once on restart.
                - A `LeaderElection`: only the elected worker runs the task.
                  It claims each fire as with `"claim"`, so a leader handover
                  never runs one fire twice. A fire missed while no worker
                  led replays once when a worker becomes the leader.

                A gated task with no schedule backend in scope reports a
                coordination error on every fire and runs nothing.
                """,
            ),
        ] = None,
        sync: Annotated[
            "LockPrimitive | None",
            Doc(
                """
                Optional resource-level synchronization primitive.

                Wraps the body once the gate lets this worker through. Use a
                `Lock` to serialise execution against a shared resource.
                Which workers run the task is set by `gate`, not this
                parameter.
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

        Every worker runs each fire by default. With `gate="claim"` or a
        `LeaderElection`, each fire is claimed against a durable last-fire
        state, so the task runs at most once across every worker per fire.
        A fire missed while every worker was down replays once on restart,
        bounded by ``misfire_grace_seconds``, and only the most recent
        missed fire runs.

        The claim guarantee is at-most-once. A worker that claims a fire and then
        crashes mid-run does not retry it, because the last-fire state already
        advanced. Make the body idempotent, or wrap it with ``@retry``, when
        correctness depends on completion.

        Raises:
            FunctionTypeError: If the task name generation fails.
            CronError: If the cron expression is invalid.
            TimezoneError: If the timezone is not an IANA timezone name.
            TypeError: If `gate` is not a supported value, or `sync` is a
                leader election.
            ValueError: If `backend` is passed without a gate.
        """
        from grelmicro.task._cron import CronTask  # noqa: PLC0415

        def decorator(
            function: Callable[[], Awaitable[None] | None],
        ) -> Callable[[], Awaitable[None] | None]:
            self.add_task(
                CronTask(
                    name=name,
                    function=function,
                    expr=expr,
                    timezone=timezone,
                    misfire_grace_seconds=misfire_grace_seconds,
                    backend=backend,
                    gate=gate,
                    sync=sync,
                ),
            )
            # `add_task` marks the same function already. This covers a
            # subclass that overrides it and schedules another way.
            mark_registered(function, Registered.TASK, self)
            return function

        return decorator

    def include_router(self, router: "TaskRouter") -> None:
        """Include another router in this router.

        Raises:
            TaskAddOperationError: If the tasks have already started.
        """
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
