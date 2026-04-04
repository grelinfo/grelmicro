"""Shared logging utilities."""

import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, tzinfo
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from grelmicro.errors import DependencyNotFoundError
from grelmicro.logging.config import (
    LoggingFormatType,
    LoggingSerializerType,
    LoggingSettings,
)
from grelmicro.logging.errors import LoggingSettingsValidationError

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover
    trace: Any = None

from grelmicro._json import has_orjson, json_default, json_dumps_str


def _stdlib_json_dumps(obj: Mapping[str, Any]) -> str:
    """Serialize object to JSON string using stdlib json.

    Always uses the standard library ``json`` module regardless of
    whether ``orjson`` is installed. Used when the user explicitly
    selects ``LOG_JSON_SERIALIZER=stdlib``.
    """
    return json.dumps(obj, separators=(",", ":"), default=json_default)


def has_opentelemetry() -> bool:
    """Check if OpenTelemetry is available."""
    return trace is not None


def get_otel_trace_context() -> dict[str, str]:
    """Extract OpenTelemetry trace context from current span.

    Returns:
        Dictionary with trace_id and span_id if active span exists,
        otherwise empty dictionary.
    """
    if not trace:
        return {}

    span = trace.get_current_span()
    span_context = span.get_span_context()

    if not span_context.is_valid:
        return {}

    return {
        "trace_id": format(span_context.trace_id, "032x"),
        "span_id": format(span_context.span_id, "016x"),
    }


# ---------------------------------------------------------------------------
# ANSI Color Support
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"

_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": "\033[34m",  # Blue
    "INFO": "\033[32m",  # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[1;31m",  # Bold Red
}

_CALLER_COLOR = "\033[36m"  # Cyan


def should_colorize() -> bool:
    """Determine whether output should use ANSI colors.

    Follows the NO_COLOR (https://no-color.org) and FORCE_COLOR conventions.
    Falls back to TTY detection on sys.stdout.
    """
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    if os.environ.get("NO_COLOR") is not None:
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def colorize_level(level: str) -> str:
    """Return the level string wrapped in its ANSI color."""
    color = _LEVEL_COLORS.get(level, "")
    if not color:
        return level
    return f"{color}{level}{_RESET}"


def dim(text: str) -> str:
    """Wrap text in ANSI dim."""
    return f"{_DIM}{text}{_RESET}"


def colorize_caller(caller: str) -> str:
    """Wrap caller in cyan."""
    return f"{_CALLER_COLOR}{caller}{_RESET}"


# ---------------------------------------------------------------------------
# Logfmt Serializer
# ---------------------------------------------------------------------------

_LOGFMT_SAFE = re.compile(r"^[a-zA-Z0-9._/@:\-+]+$")


def _logfmt_format_value(value: object) -> str:
    """Format a single value for logfmt output."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    s = str(value)
    if not s:
        return '""'
    if _LOGFMT_SAFE.match(s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


_CORE_KEYS_ORDER = (
    "time",
    "level",
    "msg",
    "logger",
    "caller",
    "trace_id",
    "span_id",
    "error",
)
_CORE_KEYS = frozenset(_CORE_KEYS_ORDER)


def _logfmt_flatten(
    record: Mapping[str, Any],
    prefix: str = "",
) -> list[tuple[str, str]]:
    """Flatten a record dict into logfmt key=value pairs.

    Nested dicts use dot notation (e.g. error.type=ValueError).
    None values are omitted.
    """
    pairs: list[tuple[str, str]] = []
    for key, value in record.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if value is None:
            continue
        if isinstance(value, dict):
            pairs.extend(_logfmt_flatten(value, full_key))
        else:
            pairs.append((full_key, _logfmt_format_value(value)))
    return pairs


def logfmt_dumps(record: Mapping[str, Any]) -> str:
    """Serialize a record dict to logfmt format.

    Core fields are emitted first in fixed order, followed by extras.
    """
    pairs: list[tuple[str, str]] = []

    for key in _CORE_KEYS_ORDER:
        if key not in record:
            continue
        value = record[key]
        if value is None:
            continue
        if isinstance(value, dict):
            pairs.extend(_logfmt_flatten(value, key))
        else:
            pairs.append((key, _logfmt_format_value(value)))

    for key, value in record.items():
        if key in _CORE_KEYS or value is None:
            continue
        if isinstance(value, dict):
            pairs.extend(_logfmt_flatten(value, key))
        else:
            pairs.append((key, _logfmt_format_value(value)))

    return " ".join(f"{k}={v}" for k, v in pairs)


# ---------------------------------------------------------------------------
# Text & Pretty Renderers
# ---------------------------------------------------------------------------

_RED = "\033[31m"


def format_extras(
    record: Mapping[str, Any], extra_skip: frozenset[str] = frozenset()
) -> str:
    """Format extra context fields as key=value pairs."""
    skip = _CORE_KEYS | extra_skip
    return " ".join(f"{k}={v}" for k, v in record.items() if k not in skip)


def render_text_line(
    record: Mapping[str, Any],
    *,
    colors: bool,
    extra_skip: frozenset[str] = frozenset(),
) -> str:
    """Render a single-line text log from a flat record dict."""
    localtime = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    level = f"{record['level']:<8}"
    logger = record["logger"]
    caller = record.get("caller")
    source = f"{logger}:{caller}" if caller else logger
    msg = record["msg"]
    extras = format_extras(record, extra_skip)
    extras_suffix = f" {extras}" if extras else ""

    if colors:
        return (
            f"{dim(localtime)} {colorize_level(level)} "
            f"{colorize_caller(source)} - {msg}"
            f"{dim(extras_suffix)}"
        )
    return f"{localtime} {level} {source} - {msg}{extras_suffix}"


def _render_pretty_field(key: str, value: object, *, colors: bool) -> str:
    """Render a single field line for pretty output."""
    if colors:
        return f"    {dim(key + ':')} {value}"
    return f"    {key}: {value}"


def _render_pretty_error(
    error: Mapping[str, Any], *, colors: bool
) -> list[str]:
    """Render error fields for pretty output."""
    lines: list[str] = [
        _render_pretty_field(
            "error.type", error.get("type", ""), colors=colors
        ),
        _render_pretty_field(
            "error.message", error.get("message", ""), colors=colors
        ),
    ]
    stack = error.get("stack")
    if stack:
        stack_lines = stack.rstrip().splitlines()
        if colors:
            lines.append(f"    {dim('error.stack:')}")
            lines.extend(f"      {_RED}{sl}{_RESET}" for sl in stack_lines)
        else:
            lines.append("    error.stack:")
            lines.extend(f"      {sl}" for sl in stack_lines)
    return lines


def render_pretty_lines(
    record: Mapping[str, Any],
    *,
    colors: bool,
    extra_skip: frozenset[str] = frozenset(),
) -> str:
    """Render multi-line pretty output from a flat record dict."""
    localtime = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    level = record["level"]
    logger = record["logger"]
    caller = record.get("caller")
    source = f"{logger}:{caller}" if caller else logger
    msg = record["msg"]
    skip = _CORE_KEYS | extra_skip

    if colors:
        lines = [
            f"  {dim(localtime)} {colorize_level(level)} {msg}",
            f"    at {colorize_caller(source)}",
        ]
    else:
        lines = [
            f"  {localtime} {level} {msg}",
            f"    at {source}",
        ]

    for key in ("trace_id", "span_id"):
        if key in record:
            lines.append(_render_pretty_field(key, record[key], colors=colors))

    for key, value in record.items():
        if key in skip:
            continue
        lines.append(_render_pretty_field(key, value, colors=colors))

    error = record.get("error")
    if error and isinstance(error, dict):
        lines.extend(_render_pretty_error(error, colors=colors))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _resolve_format(
    log_format: LoggingFormatType | str,
) -> LoggingFormatType | str:
    """Resolve AUTO format based on TTY and color environment detection."""
    if log_format == LoggingFormatType.AUTO:
        return (
            LoggingFormatType.TEXT
            if should_colorize()
            else LoggingFormatType.JSON
        )
    return log_format


class LoadedSettings(NamedTuple):
    """Validated logging settings."""

    settings: LoggingSettings
    timezone: tzinfo
    resolved_format: LoggingFormatType | str
    json_dumps: Callable[[Mapping[str, Any]], str]
    colors: bool


def load_settings() -> LoadedSettings:
    """Load and validate logging settings.

    Raises:
        DependencyNotFoundError: If orjson or OpenTelemetry is enabled but not installed.
        LoggingSettingsValidationError: If environment variables are invalid.
    """
    try:
        settings = LoggingSettings()
    except ValidationError as error:
        raise LoggingSettingsValidationError(error) from None

    json_dumps: Callable[[Mapping[str, Any]], str]
    if settings.LOG_JSON_SERIALIZER == LoggingSerializerType.ORJSON:
        if not has_orjson():
            raise DependencyNotFoundError(module="orjson")
        json_dumps = json_dumps_str  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    else:
        json_dumps = _stdlib_json_dumps

    if settings.LOG_OTEL_ENABLED and not has_opentelemetry():
        raise DependencyNotFoundError(module="opentelemetry")

    timezone = ZoneInfo(str(settings.LOG_TIMEZONE))
    resolved_format = _resolve_format(settings.LOG_FORMAT)
    colors = (
        should_colorize()
        if resolved_format in (LoggingFormatType.TEXT, LoggingFormatType.PRETTY)
        else False
    )

    return LoadedSettings(
        settings=settings,
        timezone=timezone,
        resolved_format=resolved_format,
        json_dumps=json_dumps,
        colors=colors,
    )
