"""Tasks module for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.task.manager import TaskManager

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.task.abc import Task


class Tasks:
    """Tasks module: wraps a `TaskManager` for the `Grelmicro` app.

    Registered as `micro.task` after `Grelmicro.use(Tasks())`. Exposes
    `interval(...)` and `add_task(...)` directly so users do not need to reach
    into the underlying manager for the common path.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.task import Tasks

        micro = Grelmicro(modules=[Tasks()])

        @micro.task.interval(seconds=5)
        async def cleanup() -> None: ...

        async with micro:
            await asyncio.sleep(60)
        ```

    Read more in the [Task Scheduler](../task.md) docs.
    """

    kind: ClassVar[str] = "task"

    def __init__(
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Tasks` modules may coexist on
                one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
        tasks: Annotated[
            list[Task] | None,
            Doc("Tasks to schedule at startup."),
        ] = None,
        auto_start: Annotated[
            bool,
            Doc("Start every task automatically when the module opens."),
        ] = True,
    ) -> None:
        """Initialize the module and the underlying `TaskManager`."""
        self.name = name
        self._manager = TaskManager(tasks=tasks, auto_start=auto_start)

    @property
    def manager(self) -> TaskManager:
        """The underlying `TaskManager`. Use for advanced operations not exposed here."""
        return self._manager

    def add_task(self, task: Task) -> None:
        """Schedule a task. Forwards to `TaskManager.add_task`."""
        self._manager.add_task(task)

    def interval(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Schedule a task at a fixed interval. Forwards to `TaskManager.interval`."""
        return self._manager.interval(*args, **kwargs)

    async def __aenter__(self) -> Self:
        """Open the underlying `TaskManager`."""
        await self._manager.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the underlying `TaskManager`."""
        return await self._manager.__aexit__(exc_type, exc, tb)
