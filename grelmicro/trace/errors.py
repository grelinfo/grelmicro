"""Tracing Errors."""

from grelmicro.errors import GrelmicroError, SettingsValidationError


class TraceError(GrelmicroError):
    """Base tracing error."""


class TraceSettingsValidationError(TraceError, SettingsValidationError):
    """Trace Settings Validation Error."""
