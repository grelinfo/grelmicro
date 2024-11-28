"""Grelmicro Errors."""

from typing import assert_never

from pydantic import ValidationError


class GrelmicroError(Exception):
    """Base Grelmicro error."""


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
    """Settings Validation Error."""

    def __init__(self, error: ValidationError | str) -> None:
        """Initialize the error."""
        if isinstance(error, str):
            details = error
        elif isinstance(error, ValidationError):
            details = "\n".join(
                f"- {data['loc'][0]}: {data['msg']} [input={data['input']}]"
                for data in error.errors()
            )
        else:
            assert_never(error)

        super().__init__(
            f"Could not validate environment variables settings:\n{details}"
        )
