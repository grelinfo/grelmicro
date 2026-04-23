"""Health Errors."""

from grelmicro.errors import GrelmicroError
from grelmicro.health._types import HealthDetails


class HealthError(GrelmicroError):
    """Signal a check failure. The message is exposed in the response.

    Pass ``details`` to include a diagnostic payload alongside the
    error, visible under ``details`` on the check entry in
    ``/healthz`` (subject to ``show_details``).
    """

    def __init__(
        self,
        message: str,
        *,
        details: HealthDetails | None = None,
    ) -> None:
        """Initialize with a message and optional details dict."""
        super().__init__(message)
        self.details = details


class HealthCheckTimeoutError(HealthError):
    """Raised when a health check exceeds its timeout."""

    def __init__(self, *, name: str, timeout: float) -> None:
        """Initialize the error."""
        self.name = name
        self.timeout = timeout
        super().__init__(
            f"Health check '{name}' timed out after {timeout:.1f}s"
        )
