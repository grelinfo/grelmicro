"""Logging Errors."""

from grelmicro.errors import GrelmicroError, SettingsValidationError


class LoggingError(GrelmicroError):
    """Base logging error."""


class LoggingSettingsValidationError(LoggingError, SettingsValidationError):
    """Logging Settings Validation Error."""
