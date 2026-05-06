"""Task Manager Registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro._backends import DEFAULT_NAME, BackendRegistry

if TYPE_CHECKING:
    from grelmicro.task.manager import TaskManager

task_manager_registry: BackendRegistry[TaskManager] = BackendRegistry(
    name="task"
)


def get_task_manager(name: str = DEFAULT_NAME) -> TaskManager:
    """Resolve a task manager by ``name``.

    Raises:
        BackendNotLoadedError: If no manager resolves.
    """
    return task_manager_registry.get(name)
