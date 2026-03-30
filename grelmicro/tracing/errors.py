"""Tracing Errors."""

from grelmicro.errors import GrelmicroError, SettingsValidationError


class TracingError(GrelmicroError):
    """Base tracing error."""


class TracingSettingsValidationError(TracingError, SettingsValidationError):
    """Tracing Settings Validation Error."""
