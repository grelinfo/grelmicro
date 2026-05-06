"""Task."""

from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
from grelmicro.task._backends import get_task_manager, task_manager_registry
from grelmicro.task.errors import TaskError
from grelmicro.task.manager import TaskManager
from grelmicro.task.router import TaskRouter


def register(
    manager: Annotated[TaskManager, Doc("The task manager instance.")],
    name: Annotated[
        str, Doc("Name to register the manager under.")
    ] = DEFAULT_NAME,
) -> None:
    """Register ``manager`` under ``name`` (defaults to ``"default"``)."""
    task_manager_registry.register(manager, name)


def unregister(
    name: Annotated[
        str, Doc("Name of the registered manager to remove.")
    ] = DEFAULT_NAME,
    manager: Annotated[
        TaskManager | None,
        Doc("Optional manager instance for an identity-checked removal."),
    ] = None,
) -> None:
    """Remove the registered manager under ``name``."""
    task_manager_registry.unregister(name, manager)


def use_manager(
    manager: Annotated[
        TaskManager,
        Doc("The task manager to install as the global default."),
    ],
) -> None:
    """Register ``manager`` under the ``"default"`` name."""
    task_manager_registry.register(manager, DEFAULT_NAME)


def use(
    manager: Annotated[
        TaskManager | None,
        Doc('Override the ``"default"`` slot for the duration of the block.'),
    ] = None,
    /,
    **named: TaskManager,
) -> AbstractContextManager[None]:
    """Install task-scoped manager overrides."""
    return task_manager_registry.use(manager, **named)


__all__ = [
    "TaskError",
    "TaskManager",
    "TaskRouter",
    "get_task_manager",
    "register",
    "unregister",
    "use",
    "use_manager",
]
