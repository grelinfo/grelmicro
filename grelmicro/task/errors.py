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


class TimezoneError(TaskError, ValueError):
    """Timezone Error.

    Raised when a timezone passed to `TaskRouter` or the `cron` decorator
    is not an IANA timezone name.
    """

    def __init__(self, reason: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Invalid timezone: {reason}, "
            'try an IANA timezone name such as "Europe/Zurich"'
        )


class TaskAddOperationError(TaskError, RuntimeError):
    """Raised when the task set is changed after the tasks have started.

    The default message covers adding a task or including a router. A
    caller that changes something else on a running task passes its own
    message, so the fix named is the one that applies.
    """

    def __init__(self, message: str | None = None) -> None:
        """Initialize the error."""
        super().__init__(
            message
            or "Could not add the task after the tasks started. Call "
            "'add_task' and 'include_router' before starting."
        )


class TaskStartOperationError(TaskError, RuntimeError):
    """Task Start Operation Error.

    Raised when tasks are started a second time. Every task holds its own
    schedule state, so a second run would drive the same task objects from
    two loops and report fires that never happened.
    """

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__(
            "Could not start the tasks twice, use one Tasks per application"
        )
