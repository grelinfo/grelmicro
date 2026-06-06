"""Metrics Errors."""

from grelmicro.errors import GrelmicroError


class MetricsError(GrelmicroError):
    """Base metrics error."""


class MetricsSettingsValidationError(MetricsError, ValueError):
    """Raised when the metrics configuration fails validation."""
