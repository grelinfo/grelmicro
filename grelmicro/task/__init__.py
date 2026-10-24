"""Task.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro._task import Task
    from grelmicro.task._cron import CronTask
    from grelmicro.task._fire import FireInfo, FireOutcome
    from grelmicro.task._interval import IntervalTask
    from grelmicro.task._tasks import Tasks, TasksConfig
    from grelmicro.task.errors import (
        CronError,
        FunctionTypeError,
        TaskAddOperationError,
        TaskError,
        TaskStartOperationError,
        TimezoneError,
    )
    from grelmicro.task.router import TaskRouter

__all__ = [
    "CronError",
    "CronTask",
    "FireInfo",
    "FireOutcome",
    "FunctionTypeError",
    "IntervalTask",
    "Task",
    "TaskAddOperationError",
    "TaskError",
    "TaskRouter",
    "TaskStartOperationError",
    "Tasks",
    "TasksConfig",
    "TimezoneError",
]

_LAZY: dict[str, str] = {
    "CronError": "grelmicro.task.errors",
    "CronTask": "grelmicro.task._cron",
    "FireInfo": "grelmicro.task._fire",
    "FireOutcome": "grelmicro.task._fire",
    "FunctionTypeError": "grelmicro.task.errors",
    "IntervalTask": "grelmicro.task._interval",
    "Task": "grelmicro._task",
    "TaskAddOperationError": "grelmicro.task.errors",
    "TaskError": "grelmicro.task.errors",
    "TaskRouter": "grelmicro.task.router",
    "TaskStartOperationError": "grelmicro.task.errors",
    "Tasks": "grelmicro.task._tasks",
    "TasksConfig": "grelmicro.task._tasks",
    "TimezoneError": "grelmicro.task.errors",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
