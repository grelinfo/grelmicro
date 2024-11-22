"""Grelmicro Task Scheduler Errors."""

from grelmicro.errors import GrelmicroError


class TaskError(GrelmicroError):
    """Base Grelmicro Task error."""


class FunctionTypeError(TaskError, TypeError):
    """Function Type Error."""

    def __init__(self, reference: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Could not use function {reference}, "
            "try declaring 'def' or 'async def' directly in the module"
        )


class TaskAddOperationError(TaskError, RuntimeError):
    """Task Add Operation Error."""

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__(
            "Could not add the task, try calling 'add_task' and 'include_router' before starting"
        )
