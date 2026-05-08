"""Task."""

import warnings
from typing import TYPE_CHECKING

from grelmicro.task._tasks import Tasks
from grelmicro.task.errors import TaskError
from grelmicro.task.router import TaskRouter

__all__ = ["TaskError", "TaskRouter", "Tasks"]

if TYPE_CHECKING:
    TaskManager = Tasks


def __getattr__(name: str) -> object:
    if name == "TaskManager":
        warnings.warn(
            "TaskManager is deprecated and will be removed in 1.0.0. "
            "Use Tasks instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return Tasks
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
