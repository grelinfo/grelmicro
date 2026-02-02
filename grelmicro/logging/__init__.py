"""Logging."""

from pydantic import ValidationError

from grelmicro.logging.config import LoggingBackendType, LoggingSettings
from grelmicro.logging.errors import LoggingSettingsValidationError
from grelmicro.logging.types import JSONRecordDict


def configure_logging() -> None:
    """Configure logging with the selected backend.

    Simple twelve-factor app logging configuration that logs to stdout.

    Environment Variables:
        LOG_BACKEND: Logging backend (loguru, structlog). Default: loguru
        LOG_LEVEL: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default: INFO
        LOG_FORMAT: Log format (JSON, TEXT, or custom template). Default: JSON
        LOG_TIMEZONE: IANA timezone for timestamps (e.g., "UTC", "Europe/Zurich"). Default: UTC
        LOG_OTEL_ENABLED: Enable OpenTelemetry trace context extraction.
            Default: True if OpenTelemetry is installed, else False.

    Raises:
        DependencyNotFoundError: If the selected backend module is not installed.
        LoggingSettingsValidationError: If environment variables are invalid.
    """
    try:
        settings = LoggingSettings()
    except ValidationError as error:
        raise LoggingSettingsValidationError(error) from None

    if settings.LOG_BACKEND == LoggingBackendType.STRUCTLOG:
        from grelmicro.logging._structlog import (  # noqa: PLC0415
            configure_logging as configure_structlog,
        )

        configure_structlog()
    else:
        from grelmicro.logging._loguru import (  # noqa: PLC0415
            configure_logging as configure_loguru,
        )

        configure_loguru()


__all__ = ["JSONRecordDict", "configure_logging"]
