"""Errors."""

from pydantic import ValidationError


class GrelmicroError(Exception):
    """Base grelmicro error."""


class WouldBlockError(GrelmicroError, RuntimeError):
    """Raised by a non-blocking acquire that would have blocked."""


class OutOfContextError(GrelmicroError, RuntimeError):
    """Outside Context Error.

    Raised when a method is called outside of the context manager.
    """

    def __init__(self, cls: object, method_name: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Could not call {cls.__class__.__name__}.{method_name} outside of the context manager"
        )


class DependencyNotFoundError(GrelmicroError, ImportError):
    """Dependency Not Found Error."""

    def __init__(self, *, module: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Could not import module {module}, try running 'pip install {module}'"
        )


class SettingsValidationError(GrelmicroError, ValueError):
    """Settings Validation Error.

    Pydantic ValidationError messages already describe the failure shape
    ("Input should be a valid string", "Input should be greater than 0",
    ...) so the raw input is intentionally omitted from the rendered
    error. Settings often originate from environment variables that may
    carry credentials (DSNs, tokens), and echoing the offending value
    into a log line would leak them.
    """

    def __init__(self, error: ValidationError | str) -> None:
        """Initialize the error."""
        if isinstance(error, ValidationError):
            details = "\n".join(
                f"- {data['loc'][0]}: {data['msg']}" for data in error.errors()
            )
        else:
            details = error

        super().__init__(
            f"Could not validate environment variables settings:\n{details}"
        )
