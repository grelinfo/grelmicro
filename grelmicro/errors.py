"""Grelmicro Errors."""


class FastAPICloudError(Exception):
    """Base Grelmicro error."""


class OutOfContextError(FastAPICloudError, RuntimeError):
    """Outside Context Error.

    Raised when a method is called outside of the context manager.
    """

    def __init__(self, cls: object, method_name: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"{cls.__class__.__name__}.{method_name} must be called inside the context manager.",
        )


class BackendNotLoadedError(FastAPICloudError):
    """Backend Not Loaded Error."""

    def __init__(self, backend_name: str) -> None:
        """Initialize the error."""
        super().__init__(f"No backend loaded for '{backend_name}'.")
