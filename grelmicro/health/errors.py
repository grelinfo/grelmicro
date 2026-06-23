"""Health Errors."""

from pydantic import ValidationError

from grelmicro.errors import GrelmicroError, SettingsValidationError
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


class HealthSettingsValidationError(HealthError, SettingsValidationError):
    """Health Settings Validation Error."""

    def __init__(self, error: ValidationError | str) -> None:
        """Initialize from a Pydantic validation error."""
        SettingsValidationError.__init__(self, error)
        self.details = None
