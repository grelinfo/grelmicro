"""Logging Configuration."""

from enum import StrEnum
from typing import Any, Self

from pydantic import Field
from pydantic_extra_types.timezone_name import (
    TimeZoneName,
    timezone_name_settings,
)
from pydantic_settings import BaseSettings

try:
    import opentelemetry
except ImportError:  # pragma: no cover
    opentelemetry: Any = None


class _CaseInsensitiveEnum(StrEnum):
    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        value = str(value).lower()
        for member in cls:
            if member.lower() == value:
                return member
        return None


class LoggingLevelType(_CaseInsensitiveEnum):
    """Logging Level Enum."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LoggingFormatType(_CaseInsensitiveEnum):
    """Logging Format Enum."""

    JSON = "JSON"
    TEXT = "TEXT"


class LoggingBackendType(_CaseInsensitiveEnum):
    """Logging Backend Enum."""

    LOGURU = "loguru"
    STRUCTLOG = "structlog"


@timezone_name_settings(strict=False)
class LoggingTimeZoneType(TimeZoneName):
    """Timezone name."""


class LoggingSettings(BaseSettings):
    """Logging Settings.

    Environment Variables:
        LOG_BACKEND: Logging backend (loguru, structlog). Default: loguru
        LOG_LEVEL: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default: INFO
        LOG_FORMAT: Log format (JSON, TEXT, or custom template). Default: JSON
        LOG_TIMEZONE: IANA timezone for timestamps (e.g., "UTC", "Europe/Zurich"). Default: UTC
        LOG_OTEL_ENABLED: Enable OpenTelemetry trace context extraction.
            Default: True if OpenTelemetry is installed, else False.
    """

    LOG_BACKEND: LoggingBackendType = LoggingBackendType.LOGURU
    LOG_LEVEL: LoggingLevelType = LoggingLevelType.INFO
    LOG_FORMAT: LoggingFormatType | str = Field(
        LoggingFormatType.JSON, union_mode="left_to_right"
    )
    LOG_TIMEZONE: LoggingTimeZoneType = LoggingTimeZoneType("UTC")
    LOG_OTEL_ENABLED: bool = opentelemetry is not None
