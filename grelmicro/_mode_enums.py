"""The mode enums a config field took before it took a string literal.

Each one stays importable from its old location until 0.44.0, and every
access warns with a `GrelmicroFutureWarning`. A member is a `str`, so a
config field still accepts it. Until 0.44.0 a field reads back as the
member, so `is` keeps working, and `.value` and `.name` work and warn.
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any, Self
from warnings import deprecated

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from grelmicro._deprecation import MEMBER_ATTRIBUTES, MODE_ENUMS


class _CaseInsensitiveEnum(StrEnum):
    @enum.property
    def value(self) -> str:
        """The member's string, with a warning that it is removed in 0.44.0."""
        MEMBER_ATTRIBUTES[type(self).__name__, "value"].warn(stacklevel=3)
        return self._value_

    @enum.property
    def name(self) -> str:
        """The member's name, with a warning that it is removed in 0.44.0."""
        MEMBER_ATTRIBUTES[type(self).__name__, "name"].warn(stacklevel=3)
        return self._name_

    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        if not isinstance(value, str):
            return None
        value = value.lower()
        for member in cls:
            if member.lower() == value:
                return member
        return None


@deprecated(
    "`MetricsExporterType` is removed in 0.44.0. "
    'Use `"prometheus"` and the `MetricsExporter` type instead.',
    category=None,
)
class MetricsExporterType(_CaseInsensitiveEnum):
    """Metric exporter selection."""

    AUTO = "auto"
    OTLP_HTTP = "otlp-http"
    OTLP_GRPC = "otlp-grpc"
    PROMETHEUS = "prometheus"
    CONSOLE = "console"
    NONE = "none"


@deprecated(
    "`TraceExporterType` is removed in 0.44.0. "
    'Use `"otlp-http"` and the `TraceExporter` type instead.',
    category=None,
)
class TraceExporterType(_CaseInsensitiveEnum):
    """Span exporter selection."""

    AUTO = "auto"
    OTLP_HTTP = "otlp-http"
    OTLP_GRPC = "otlp-grpc"
    CONSOLE = "console"
    NONE = "none"


@deprecated(
    "`TraceProcessorType` is removed in 0.44.0. "
    'Use `"batch"` and the `TraceProcessor` type instead.',
    category=None,
)
class TraceProcessorType(_CaseInsensitiveEnum):
    """Span processor selection."""

    BATCH = "batch"
    SIMPLE = "simple"


@deprecated(
    "`TraceSamplerType` is removed in 0.44.0. "
    'Use `"parentbased_always_on"` and the `TraceSampler` type instead.',
    category=None,
)
class TraceSamplerType(_CaseInsensitiveEnum):
    """Sampler selection."""

    ALWAYS_ON = "always_on"
    ALWAYS_OFF = "always_off"
    PARENTBASED_ALWAYS_ON = "parentbased_always_on"
    TRACEIDRATIO = "traceidratio"


@deprecated(
    "`LogLevelType` is removed in 0.44.0. "
    'Use `"INFO"` and the `LogLevel` type instead.',
    category=None,
)
class LogLevelType(_CaseInsensitiveEnum):
    """Log Level Enum."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@deprecated(
    "`LogFormatType` is removed in 0.44.0. "
    'Use `"JSON"` and the `LogFormat` type instead.',
    category=None,
)
class LogFormatType(_CaseInsensitiveEnum):
    """Log Format Enum."""

    AUTO = "AUTO"
    JSON = "JSON"
    LOGFMT = "LOGFMT"
    TEXT = "TEXT"
    PRETTY = "PRETTY"


@deprecated(
    "`LogBackendType` is removed in 0.44.0. "
    'Use `"loguru"` and the `LogBackend` type instead.',
    category=None,
)
class LogBackendType(_CaseInsensitiveEnum):
    """Log Backend Enum."""

    LOGURU = "loguru"
    STRUCTLOG = "structlog"
    STDLIB = "stdlib"


@deprecated(
    "`LogSerializerType` is removed in 0.44.0. "
    'Use `"orjson"` and the `LogSerializer` type instead.',
    category=None,
)
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


def _as_str(value: str) -> str:
    """Return `value` as a plain `str`, without reading a member's `.value`."""
    return str.__str__(value)


@dataclass(frozen=True, slots=True)
class _MemberOf:
    """Read a choice back as a member, and dump it to JSON as a plain `str`."""

    members: Mapping[Any, Enum]

    def read_back(self, value: Any) -> Any:  # noqa: ANN401
        """Return the member `value` names, or `value` when it names none."""
        return self.members.get(value, value)

    def __get_pydantic_core_schema__(
        self,
        source: object,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Wrap the field schema with the read back and the JSON dump."""
        return core_schema.no_info_after_validator_function(
            self.read_back,
            handler(source),
            serialization=core_schema.plain_serializer_function_ser_schema(
                _as_str, when_used="json"
            ),
        )


def member_of(name: str) -> _MemberOf:
    """Read a validated choice back as its member of the mode enum `name`.

    A value that names no member, such as a custom log format template,
    passes through unchanged. A JSON dump writes the plain string.
    """
    return _MemberOf(_ENUMS[name]._value2member_map_)
