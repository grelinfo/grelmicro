"""Health Errors."""

from grelmicro.errors import GrelmicroError


class HealthError(GrelmicroError):
    """Base health error."""


class HealthCheckTimeoutError(HealthError):
    """Raised when a health check exceeds its timeout."""

    def __init__(self, *, name: str, timeout: float) -> None:
        """Initialize the error."""
        self.name = name
        self.timeout = timeout
        super().__init__(
            f"Health check '{name}' timed out after {timeout:.1f}s"
        )
