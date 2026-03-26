"""Cache Errors."""

from grelmicro.errors import GrelmicroError, SettingsValidationError


class CacheError(GrelmicroError):
    """Base cache error."""


class CacheSettingsValidationError(CacheError, SettingsValidationError):
    """Cache Settings Validation Error."""
