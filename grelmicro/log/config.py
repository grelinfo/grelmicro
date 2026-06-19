"""Logging Configuration."""

from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, Field
from pydantic_extra_types.timezone_name import (
    TimeZoneName,
    timezone_name_settings,
)
from typing_extensions import Doc

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

    AUTO = "AUTO"
    JSON = "JSON"
    LOGFMT = "LOGFMT"
    TEXT = "TEXT"
    PRETTY = "PRETTY"


class LoggingBackendType(_CaseInsensitiveEnum):
    """Logging Backend Enum."""

    LOGURU = "loguru"
    STRUCTLOG = "structlog"
    STDLIB = "stdlib"


class LoggingSerializerType(_CaseInsensitiveEnum):
    """JSON Serializer Enum."""

    STDLIB = "stdlib"
    ORJSON = "orjson"


@timezone_name_settings(strict=False)
class LoggingTimeZoneType(TimeZoneName):
    """Timezone name."""


class LoggingConfig(BaseModel, frozen=True, extra="forbid"):
    """Logging Config."""

    backend: Annotated[
        LoggingBackendType,
        Doc("Logging backend implementation."),
    ] = LoggingBackendType.STDLIB
    level: Annotated[
        LoggingLevelType,
        Doc("Log level threshold."),
    ] = LoggingLevelType.INFO
    format: Annotated[
        LoggingFormatType | str,
        Doc("Log format. Built-in option or a custom template string."),
        Field(union_mode="left_to_right"),
    ] = LoggingFormatType.AUTO
    timezone: Annotated[
        LoggingTimeZoneType,  # ty: ignore[invalid-type-form]
        Doc("IANA timezone for timestamps."),
    ] = LoggingTimeZoneType("UTC")
    json_serializer: Annotated[
        LoggingSerializerType,
        Doc("JSON serializer used for structured output."),
    ] = LoggingSerializerType.STDLIB
    caller_enabled: Annotated[
        bool,
        Doc("Include caller (function and line) in log records."),
    ] = False
    otel_enabled: Annotated[
        bool,
        Doc("Extract OpenTelemetry trace context into log records."),
    ] = opentelemetry is not None
