"""Task Errors."""

from grelmicro.errors import GrelmicroError


class TaskError(GrelmicroError):
    """Base grelmicro Task error."""


class FunctionTypeError(TaskError, TypeError):
    """Function Type Error."""

    def __init__(self, reference: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Could not use function {reference}, "
            "try declaring 'def' or 'async def' directly in the module"
        )


class CronError(TaskError, ValueError):
    """Cron Expression Error.

    Raised when a cron expression is malformed or describes a schedule that
    never matches a real date.
    """

    def __init__(self, reason: str) -> None:
        """Initialize the error."""
        super().__init__(f"Invalid cron expression: {reason}")


class TaskAddOperationError(TaskError, RuntimeError):
    """Task Add Operation Error."""

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__(
            "Could not add the task, try calling 'add_task' and 'include_router' before starting"
        )
