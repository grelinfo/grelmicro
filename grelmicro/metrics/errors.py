"""Metrics Errors."""

from grelmicro.errors import GrelmicroError, SettingsValidationError


class MetricsError(GrelmicroError):
    """Base metrics error."""


class MetricsSettingsValidationError(MetricsError, SettingsValidationError):
    """Raised when the metrics configuration fails validation."""
