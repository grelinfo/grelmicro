"""Tracing Errors."""

from grelmicro.errors import GrelmicroError, SettingsValidationError


class TraceError(GrelmicroError):
    """Base trace error."""


class TraceSettingsValidationError(TraceError, SettingsValidationError):
    """Trace Settings Validation Error."""
