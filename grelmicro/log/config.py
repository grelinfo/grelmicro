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


class LogLevelType(_CaseInsensitiveEnum):
    """Log Level Enum."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogFormatType(_CaseInsensitiveEnum):
    """Log Format Enum."""

    AUTO = "AUTO"
    JSON = "JSON"
    LOGFMT = "LOGFMT"
    TEXT = "TEXT"
    PRETTY = "PRETTY"


class LogBackendType(_CaseInsensitiveEnum):
    """Log Backend Enum."""

    LOGURU = "loguru"
    STRUCTLOG = "structlog"
    STDLIB = "stdlib"


class LogSerializerType(_CaseInsensitiveEnum):
    """JSON Serializer Enum."""

    STDLIB = "stdlib"
    ORJSON = "orjson"


@timezone_name_settings(strict=False)
class LogTimeZoneType(TimeZoneName):
    """Timezone name."""


class LogConfig(BaseModel, frozen=True, extra="forbid"):
    """Log Config."""

    backend: Annotated[
        LogBackendType,
        Doc("Logging backend implementation."),
    ] = LogBackendType.STDLIB
    level: Annotated[
        LogLevelType,
        Doc("Log level threshold."),
    ] = LogLevelType.INFO
    format: Annotated[
        LogFormatType | str,
        Doc("Log format. Built-in option or a custom template string."),
        Field(union_mode="left_to_right"),
    ] = LogFormatType.AUTO
    timezone: Annotated[
        LogTimeZoneType,  # ty: ignore[invalid-type-form]
        Doc("IANA timezone for timestamps."),
    ] = LogTimeZoneType("UTC")
    json_serializer: Annotated[
        LogSerializerType,
        Doc("JSON serializer used for structured output."),
    ] = LogSerializerType.STDLIB
    caller_enabled: Annotated[
        bool,
        Doc("Include caller (function and line) in log records."),
    ] = False
    otel_enabled: Annotated[
        bool,
        Doc("Extract OpenTelemetry trace context into log records."),
    ] = opentelemetry is not None
