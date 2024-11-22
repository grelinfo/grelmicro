"""Grelmicro Backend Errors."""

from grelmicro.errors import GrelmicroError


class BackendNotLoadedError(GrelmicroError):
    """Backend Not Loaded Error."""

    def __init__(self, backend_name: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Could not load backend {backend_name}, try initializing one first"
        )
