"""Grelmicro Errors."""


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


class DependencyNotFoundError(GrelmicroError):
    """Dependency Not Found Error."""

    def __init__(self, module: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Could not import module {module}, try running 'pip install {module}'"
        )
