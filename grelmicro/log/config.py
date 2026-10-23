"""Logging Configuration."""

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field
from typing_extensions import Doc

from grelmicro._config import any_case
from grelmicro._mode_enums import member_of
from grelmicro._timezone import UTC_NAME
from grelmicro.types import LogLevel, TimeZoneName

try:
    import opentelemetry
except ImportError:  # pragma: no cover
    opentelemetry: Any = None  # type: ignore[no-redef]

if TYPE_CHECKING:
    from grelmicro._mode_enums import (
        LogBackendType as LogBackendType,  # noqa: PLC0414
    )
    from grelmicro._mode_enums import (
        LogFormatType as LogFormatType,  # noqa: PLC0414
    )
    from grelmicro._mode_enums import (
        LogLevelType as LogLevelType,  # noqa: PLC0414
    )
    from grelmicro._mode_enums import (
        LogSerializerType as LogSerializerType,  # noqa: PLC0414
    )

type LogFormat = Literal["AUTO", "JSON", "LOGFMT", "TEXT", "PRETTY"]
"""Built-in log format, matched in any case."""

type LogBackend = Literal["loguru", "structlog", "stdlib"]
"""Logging backend selection, matched in any case."""

type LogSerializer = Literal["auto", "stdlib", "orjson"]
"""JSON serializer selection, matched in any case."""

_MODE_ENUMS = frozenset(
    {"LogBackendType", "LogFormatType", "LogLevelType", "LogSerializerType"}
)
"""The mode enums removed in 0.44.0."""


class LogConfig(BaseModel, frozen=True, extra="forbid"):
    """Log Config."""

    backend: Annotated[
        LogBackend,
        any_case(LogBackend),
        member_of("LogBackendType"),
        Field(validate_default=True),
        Doc("Logging backend implementation."),
    ] = "stdlib"
    level: Annotated[
        LogLevel,
        any_case(LogLevel),
        member_of("LogLevelType"),
        Field(validate_default=True),
        Doc("Log level threshold."),
    ] = "INFO"
    format: Annotated[
        LogFormat | str,
        Doc("Log format. Built-in option or a custom template string."),
        Field(union_mode="left_to_right", validate_default=True),
        any_case(LogFormat),
        member_of("LogFormatType"),
    ] = "AUTO"
    timezone: Annotated[
        TimeZoneName,
        Doc("IANA timezone for timestamps."),
    ] = TimeZoneName(UTC_NAME)
    json_serializer: Annotated[
        LogSerializer,
        any_case(LogSerializer),
        member_of("LogSerializerType"),
        Field(validate_default=True),
        Doc(
            "JSON serializer used for structured output. `auto` uses "
            "orjson when it is installed and the standard library "
            "otherwise. `orjson` requires it and raises when it is "
            "missing."
        ),
    ] = "auto"
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
            "Take over uvicorn's own loggers so they match this format and "
            "write to the same stream. Uvicorn installs its own handlers with "
            "propagation off, so without this its lines keep their own format "
            "and the process emits two. Turn it off only when something "
            "other than grelmicro owns uvicorn's logging."
        ),
    ] = True
    queue_enabled: Annotated[
        bool,
        Doc(
            "Hand log writes to a background thread. The record is "
            "rendered on the calling thread and queued, so a slow sink "
            "no longer blocks the event loop. A full queue drops the "
            "arriving record and reports the count."
        ),
    ] = False
    queue_size: Annotated[
        int,
        Doc("How many rendered records the queue holds before dropping."),
        Field(gt=0),
    ] = 10_000


def __getattr__(name: str) -> object:
    """Return a mode enum removed in 0.44.0, with a warning."""
    if name in _MODE_ENUMS:
        from grelmicro._mode_enums import deprecated_enum  # noqa: PLC0415

        return deprecated_enum(name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
