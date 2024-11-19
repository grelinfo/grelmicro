"""Grelmicro Task Scheduler Errors."""

from grelmicro.errors import FastAPICloudError


class TaskError(FastAPICloudError):
    """Base Grelmicro Task error."""


class FunctionNotSupportedError(TaskError):
    """The function is not supported."""

    def __init__(self, reference: str) -> None:
        """Initialize the error."""
        super().__init__(f"The function type is not supported: {reference}")


class TaskAlreadyStartedError(TaskError, RuntimeError):
    """Operation Error because the task is already started."""

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__(
            "Task already started: 'add_task' and 'include_router' are not allowed.",
        )
