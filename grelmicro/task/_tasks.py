"""Tasks."""

import asyncio
from contextlib import AsyncExitStack
from logging import getLogger
from types import TracebackType
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, NonNegativeFloat
from typing_extensions import Doc

from grelmicro._config import (
    Reconfigurable,
    default_env_prefix,
    resolve_config,
)
from grelmicro._timezone import SHARED_TIMEZONE_ENV, UTC_NAME
from grelmicro.errors import OutOfContextError
from grelmicro.task._protocol import Task
from grelmicro.task.errors import (
    TaskSettingsValidationError,
    TaskStartOperationError,
)
from grelmicro.task.router import TaskRouter
from grelmicro.types import TimeZoneName

logger = getLogger("grelmicro.task")


class TasksConfig(BaseModel, frozen=True, extra="forbid"):
    """Tasks Config."""

    timezone: Annotated[
        TimeZoneName,
        Doc(
            "IANA timezone every cron task uses unless it, or the "
            "`TaskRouter` holding it, sets its own."
        ),
    ] = TimeZoneName(UTC_NAME)
    shutdown_timeout: Annotated[
        NonNegativeFloat,
        Doc(
            "Seconds to let running tasks finish their current unit of "
            "work on shutdown before they are force-cancelled."
        ),
    ] = 30.0


class Tasks(TaskRouter, Reconfigurable[TasksConfig]):
    """Tasks.

    `Tasks` class, the main entrypoint to manage scheduled tasks.

    Supports live reconfiguration of `shutdown_timeout` via
    `reconfigure(new_config)`. A swap applies to the next shutdown, not
    to a drain already under way. `timezone` is startup-only: changing
    the zone under a running cron task would move which wall-clock fire
    counts as due, and the durable last-fire state would either replay a
    fire or swallow one. See
    [Live reconfiguration](../architecture/reconfigure.md).
    """

    _IMMUTABLE_RECONFIGURE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"timezone"}
    )

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
        timezone: Annotated[
            str | None,
            Doc(
                """
                The IANA timezone name every cron task uses.

                A cron task that passes its own ``timezone=``, and a
                `TaskRouter` that declares one, keep theirs.

                Default: ``"UTC"``. When unset and env reads are enabled
                (see ``env_load`` and ``GREL_ENV_LOAD``), resolves from
                ``GREL_TASK_TIMEZONE``, then from the app-wide
                ``GREL_TIMEZONE``, before falling back to the default.
                """,
            ),
        ] = None,
        shutdown_timeout: Annotated[
            float | None,
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

                When unset and env reads are enabled, resolves from
                ``GREL_TASK_SHUTDOWN_TIMEOUT``.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: ``GREL_TASK_``. `Tasks` takes no registration
                name, so two instances in one process read the same
                variables. Pass a prefix here to separate them.
                """
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_ENV_LOAD`` flag. Pass True or False to override
                the flag for this construction.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize Tasks.

        Raises:
            TaskSettingsValidationError: If a setting fails validation.
        """
        config = resolve_config(
            TasksConfig,
            explicit=None,
            kwargs={
                "timezone": timezone,
                "shutdown_timeout": shutdown_timeout,
            },
            env_prefix=env_prefix or default_env_prefix("TASK", "default"),
            env_load=env_load,
            shared_env=SHARED_TIMEZONE_ENV,
            error_type=TaskSettingsValidationError,
        )
        self._setup(config, auto_start=auto_start, tasks=tasks)
        self._track_reconfigure(
            env_prefix or default_env_prefix("TASK", "default")
        )

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            TasksConfig,
            Doc(
                """
                The pre-built tasks configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault,
                or a ``pydantic-settings`` aggregator). The environment
                path is bypassed and the config is used as-is.
                """
            ),
        ],
        *,
        auto_start: Annotated[
            bool,
            Doc("Automatically start all tasks."),
        ] = True,
        tasks: Annotated[
            list[Task] | None,
            Doc("A list of tasks to be started."),
        ] = None,
    ) -> Self:
        """Construct a `Tasks` from a pre-built `TasksConfig`."""
        instance = cls.__new__(cls)
        instance._setup(config, auto_start=auto_start, tasks=tasks)  # noqa: SLF001
        return instance

    def _setup(
        self,
        config: TasksConfig,
        *,
        auto_start: bool,
        tasks: list[Task] | None,
    ) -> None:
        """Wire the validated config and runtime state onto the instance."""
        TaskRouter.__init__(self, tasks=tasks, timezone=config.timezone)
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._auto_start = auto_start
        self._task_group: asyncio.TaskGroup | None = None
        self._task_handles: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    @property
    def timezone(self) -> str:
        """The IANA timezone name cron tasks use unless they set their own."""
        return self._config.timezone

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
        if handles and self._config.shutdown_timeout > 0:
            _, pending = await asyncio.wait(
                handles, timeout=self._config.shutdown_timeout
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
            raise TaskStartOperationError

        # Resolve before marking as started: `_set_default_timezone`
        # refuses to move a task that is already running.
        self._resolve_timezones(self._config.timezone)
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
