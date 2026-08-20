"""Logging Configuration."""

from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, Field
from typing_extensions import Doc

from grelmicro._timezone import UTC_NAME
from grelmicro.types import TimeZoneName

try:
    import opentelemetry
except ImportError:  # pragma: no cover
    opentelemetry: Any = None  # type: ignore[no-redef]


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

    AUTO = "auto"
    STDLIB = "stdlib"
    ORJSON = "orjson"


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
        TimeZoneName,
        Doc("IANA timezone for timestamps."),
    ] = TimeZoneName(UTC_NAME)
    json_serializer: Annotated[
        LogSerializerType,
        Doc(
            "JSON serializer used for structured output. `auto` uses "
            "orjson when it is installed and the standard library "
            "otherwise. `orjson` requires it and raises when it is "
            "missing."
        ),
    ] = LogSerializerType.AUTO
    caller_enabled: Annotated[
        bool,
        Doc("Include caller (function and line) in log records."),
    ] = False
    otel_enabled: Annotated[
        bool,
        Doc("Extract OpenTelemetry trace context into log records."),
    ] = opentelemetry is not None
    uvicorn_enabled: Annotated[
        bool,
        Doc(
            "Reformat uvicorn's own loggers to match this format. Uvicorn "
            "installs its own handlers with propagation off, so without this "
            "its lines keep their own format and the process emits two."
        ),
    ] = True
