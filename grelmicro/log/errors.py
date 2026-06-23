"""Logging Errors."""

from grelmicro.errors import GrelmicroError, SettingsValidationError


class LogError(GrelmicroError):
    """Base log error."""


class LogSettingsValidationError(LogError, SettingsValidationError):
    """Log Settings Validation Error."""
