"""Logging Configuration."""

from typing import Annotated, Any, Literal, Self, get_args

from pydantic import AfterValidator, BaseModel, Field, PlainSerializer
from typing_extensions import Doc

from grelmicro._config import any_case
from grelmicro._deprecation import format_case
from grelmicro._timezone import UTC_NAME
from grelmicro.types import LogLevel, TimeZoneName

try:
    import opentelemetry
except ImportError:  # pragma: no cover
    opentelemetry: Any = None  # type: ignore[no-redef]

type LogFormat = Literal[
    "auto",
    "json",
    "logfmt",
    "text",
    "pretty",
    "AUTO",
    "JSON",
    "LOGFMT",
    "TEXT",
    "PRETTY",
]
"""Built-in log format, matched in any case.

Type checkers accept the uppercase spellings until 0.45.0.
"""

_BuiltinFormat = Literal["auto", "json", "logfmt", "text", "pretty"]
"""The built-in formats, in the documented spelling."""

type LogBackend = Literal["loguru", "structlog", "stdlib"]
"""Logging backend selection, matched in any case."""

type LogSerializer = Literal["auto", "stdlib", "orjson"]
"""JSON serializer selection, matched in any case."""


_FORMAT_NAMES: frozenset[str] = frozenset(get_args(_BuiltinFormat))
"""The built-in format names, in lowercase."""


class _UppercaseFormat(str):
    """A built-in format read back in uppercase, such as `"JSON"`, until 0.45.0.

    Comparing it with a built-in format name in any case warns with a
    `Grelmicro045Warning`. Every other use behaves as the plain `str`.
    """

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        """Compare as a `str`, and warn when `other` names a built-in format."""
        _warn_on_format_name(other)
        return str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        """Compare as a `str`, and warn when `other` names a built-in format."""
        _warn_on_format_name(other)
        return str.__ne__(self, other)

    __hash__ = str.__hash__

    def __reduce__(self) -> tuple[type[str], tuple[str]]:
        """Pickle as the plain `str`."""
        return (str, (str(self),))

    def __copy__(self) -> Self:
        """Return the same value, as `str` does."""
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        """Return the same value, as `str` does."""
        return self


_DEFAULT_FORMAT: str = str(_UppercaseFormat("AUTO"))
"""The `LogConfig.format` default, a plain `"AUTO"`.

Comparing a read-back format with this object warns nothing, so a dump that
excludes defaults stays silent.
"""


def _warn_on_format_name(other: object) -> None:
    """Warn at the comparison when `other` names a built-in format."""
    if other is _DEFAULT_FORMAT:
        return
    if isinstance(other, str) and not isinstance(other, _UppercaseFormat):
        name = other.lower()
        if name in _FORMAT_NAMES:
            format_case(name).warn(stacklevel=3)


def _read_back_format(value: str) -> str:
    """Return a built-in format in uppercase, and a template as written."""
    if value in _FORMAT_NAMES:
        return _UppercaseFormat(value.upper())
    return value


class LogConfig(BaseModel, frozen=True, extra="forbid"):
    """Log Config."""

    backend: Annotated[
        LogBackend,
        any_case(LogBackend),
        Field(validate_default=True),
        Doc("Logging backend implementation."),
    ] = "stdlib"
    level: Annotated[
        LogLevel,
        any_case(LogLevel),
        Field(validate_default=True),
        Doc("Log level threshold."),
    ] = "INFO"
    format: Annotated[
        LogFormat | str,
        Doc("Log format. Built-in option or a custom template string."),
        Field(union_mode="left_to_right", validate_default=True),
        any_case(_BuiltinFormat),
        AfterValidator(_read_back_format),
        PlainSerializer(str, return_type=str, when_used="always"),
    ] = _DEFAULT_FORMAT
    timezone: Annotated[
        TimeZoneName,
        Doc("IANA timezone for timestamps."),
    ] = TimeZoneName(UTC_NAME)
    json_serializer: Annotated[
        LogSerializer,
        any_case(LogSerializer),
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
