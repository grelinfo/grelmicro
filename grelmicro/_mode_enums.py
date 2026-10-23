"""The mode enums a config field took before it took a string literal.

Each one stays importable from its old location until 0.44.0, and every
access warns with a `GrelmicroFutureWarning`. A member is a `str`, so a
config field still accepts it. Until 0.44.0 a field reads back as the
member, so `.value` and `is` keep working.
"""

from enum import StrEnum
from typing import Any, Self

from pydantic import AfterValidator

from grelmicro._deprecation import MODE_ENUMS


class _CaseInsensitiveEnum(StrEnum):
    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        if not isinstance(value, str):
            return None
        value = value.lower()
        for member in cls:
            if member.lower() == value:
                return member
        return None


class MetricsExporterType(_CaseInsensitiveEnum):
    """Metric exporter selection."""

    AUTO = "auto"
    OTLP_HTTP = "otlp-http"
    OTLP_GRPC = "otlp-grpc"
    PROMETHEUS = "prometheus"
    CONSOLE = "console"
    NONE = "none"


class TraceExporterType(_CaseInsensitiveEnum):
    """Span exporter selection."""

    AUTO = "auto"
    OTLP_HTTP = "otlp-http"
    OTLP_GRPC = "otlp-grpc"
    CONSOLE = "console"
    NONE = "none"


class TraceProcessorType(_CaseInsensitiveEnum):
    """Span processor selection."""

    BATCH = "batch"
    SIMPLE = "simple"


class TraceSamplerType(_CaseInsensitiveEnum):
    """Sampler selection."""

    ALWAYS_ON = "always_on"
    ALWAYS_OFF = "always_off"
    PARENTBASED_ALWAYS_ON = "parentbased_always_on"
    TRACEIDRATIO = "traceidratio"


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


_ENUMS: dict[str, type[StrEnum]] = {
    enum.__name__: enum
    for enum in (
        MetricsExporterType,
        TraceExporterType,
        TraceProcessorType,
        TraceSamplerType,
        LogLevelType,
        LogFormatType,
        LogBackendType,
        LogSerializerType,
    )
}
"""Each mode enum, by name."""


def deprecated_enum(name: str) -> type[StrEnum]:
    """Return the mode enum `name` and warn that it is removed.

    Call it from a module `__getattr__`, so the warning reports the line
    that imported or read the name.
    """
    MODE_ENUMS[name].warn(stacklevel=3)
    return _ENUMS[name]


def member_of(name: str) -> AfterValidator:
    """Read a validated choice back as its member of the mode enum `name`.

    A value that names no member, such as a custom log format template,
    passes through unchanged.
    """
    members = _ENUMS[name]._value2member_map_

    def read_back(value: Any) -> Any:  # noqa: ANN401
        return members.get(value, value)

    return AfterValidator(read_back)
