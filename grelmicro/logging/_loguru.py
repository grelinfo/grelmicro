"""Loguru Logging Backend."""

import sys
import traceback as tb_module
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any

from grelmicro._context import merge_context_into as _merge_context_into
from grelmicro.logging._shared import (
    _stdlib_json_dumps,
    get_otel_trace_context,
    load_settings,
    logfmt_dumps,
    render_pretty_lines,
    render_text_line,
)
from grelmicro.logging.config import LoggingFormatType
from grelmicro.logging.types import ErrorDict

try:
    import loguru
except ImportError as exc:  # pragma: no cover
    msg = "loguru is required for the loguru logging backend"
    raise ImportError(msg) from exc

if TYPE_CHECKING:
    from loguru import FormatFunction, Record

_LOGURU_INTERNAL_KEYS = frozenset(
    {
        "serialized",
        "localtime",
        "logfmt_serialized",
    }
)

JSON_FORMAT = "{extra[serialized]}"
LOGFMT_FORMAT = "{extra[logfmt_serialized]}"


def _build_loguru_record(
    record: "Record",
    timezone: tzinfo,
) -> dict[str, Any]:
    """Build a structured record dict from a loguru Record."""
    # Context fields < log extras < core fields (last wins)
    log_record: dict[str, Any] = {}
    _merge_context_into(log_record)
    log_record.update(
        {
            k: v
            for k, v in record["extra"].items()
            if k not in _LOGURU_INTERNAL_KEYS
        }
    )
    # combine() converts loguru's datetime subclass to stdlib datetime (orjson compat)
    ldt = record["time"]
    log_record["time"] = datetime.combine(
        ldt.date(), ldt.time(), tzinfo=ldt.tzinfo
    ).astimezone(timezone)
    log_record["level"] = record["level"].name
    log_record["msg"] = record["message"]
    log_record["logger"] = record["name"]
    log_record["caller"] = f"{record['function']}:{record['line']}"

    # trace_id/span_id already merged via dict.update from record["extra"]
    exception = record["exception"]
    if exception and exception.type:
        error = ErrorDict(
            type=exception.type.__name__,
            message=str(exception.value),
        )
        if exception.traceback:
            error["stack"] = "".join(
                tb_module.format_exception(
                    exception.type, exception.value, exception.traceback
                )
            )
        log_record["error"] = error

    return log_record


class _LoguruPatcher:
    """Internal patcher for enriching loguru records.

    Args:
        json_dumps: JSON serializer function. Falls back to ``_stdlib_json_dumps``
            when ``None`` (the default). Must be provided explicitly when
            ``enable_json=True`` and a non-stdlib serializer is desired.
    """

    def __init__(
        self,
        *,
        timezone: tzinfo = UTC,
        enable_localtime: bool = False,
        enable_json: bool = False,
        enable_logfmt: bool = False,
        enable_otel: bool = False,
        json_dumps: Callable[[Mapping[str, Any]], str] | None = None,
    ) -> None:
        self.timezone = timezone
        self.enable_localtime = enable_localtime
        self.enable_json = enable_json
        self.enable_logfmt = enable_logfmt
        self.enable_otel = enable_otel
        self.json_dumps = json_dumps

    def __call__(self, record: "Record") -> None:
        if self.enable_otel:
            _otel_patcher(record)
        if self.enable_localtime:
            _localtime_patcher(record, timezone=self.timezone)
        if self.enable_json:
            _json_patcher(
                record, timezone=self.timezone, json_dumps=self.json_dumps
            )
        if self.enable_logfmt:
            _logfmt_patcher(record, timezone=self.timezone)


def _json_patcher(
    record: "Record",
    *,
    timezone: tzinfo | None = None,
    json_dumps: Callable[[Mapping[str, Any]], str] | None = None,
) -> None:
    """Patch the record with JSON serialization."""
    serializer = json_dumps or _stdlib_json_dumps
    tz = timezone or UTC
    log_record = _build_loguru_record(record, tz)
    record["extra"]["serialized"] = serializer(log_record)


def _logfmt_patcher(
    record: "Record",
    *,
    timezone: tzinfo | None = None,
) -> None:
    """Patch the record with logfmt serialization."""
    tz = timezone or UTC
    log_record = _build_loguru_record(record, tz)
    record["extra"]["logfmt_serialized"] = logfmt_dumps(log_record)


def _localtime_patcher(
    record: "Record",
    *,
    timezone: tzinfo | None = None,
) -> None:
    """Patch the record with localized time (format: YYYY-MM-DD HH:MM:SS.mmm)."""
    record["extra"]["localtime"] = (
        record["time"]
        .astimezone(timezone or UTC)
        .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    )


def _otel_patcher(record: "Record") -> None:
    """Patch the record with OpenTelemetry trace context (no-op if unavailable)."""
    trace_context = get_otel_trace_context()
    if trace_context:
        record["extra"]["trace_id"] = trace_context["trace_id"]
        record["extra"]["span_id"] = trace_context["span_id"]


def _json_formatter(record: "Record") -> str:  # noqa: ARG001
    """Return pre-serialized JSON from patcher. Suppresses loguru's auto-traceback."""
    return JSON_FORMAT + "\n"


def _logfmt_formatter(record: "Record") -> str:  # noqa: ARG001
    """Return pre-serialized logfmt from patcher. Suppresses loguru's auto-traceback."""
    return LOGFMT_FORMAT + "\n"


def _escape_loguru_tags(text: str) -> str:
    """Escape angle brackets so loguru's colorizer does not interpret them."""
    return text.replace("<", r"\<")


def _make_text_formatter(
    timezone: tzinfo,
    *,
    colors: bool,
) -> "FormatFunction":
    """Create a text format function with captured settings."""

    def _formatter(record: "Record") -> str:
        log_record = _build_loguru_record(record, timezone)
        return (
            _escape_loguru_tags(render_text_line(log_record, colors=colors))
            + "\n"
        )

    return _formatter


def _make_pretty_formatter(
    timezone: tzinfo,
    *,
    colors: bool,
) -> "FormatFunction":
    """Create a pretty format function with captured settings."""

    def _formatter(record: "Record") -> str:
        log_record = _build_loguru_record(record, timezone)
        return (
            _escape_loguru_tags(render_pretty_lines(log_record, colors=colors))
            + "\n"
        )

    return _formatter


def configure_logging() -> None:
    """Configure logging with loguru.

    Simple twelve-factor app logging configuration that logs to stdout.

    Environment Variables:
        LOG_LEVEL: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default: INFO
        LOG_FORMAT: Log format (AUTO, JSON, LOGFMT, TEXT, PRETTY). Default: AUTO
        LOG_TIMEZONE: IANA timezone for timestamps (e.g., "UTC", "Europe/Zurich"). Default: UTC
        LOG_OTEL_ENABLED: Enable OpenTelemetry trace context extraction.
            Default: True if OpenTelemetry is installed, else False.

    Raises:
        DependencyNotFoundError: If OpenTelemetry is enabled but not installed.
        LoggingSettingsValidationError: If environment variables are invalid.
    """
    settings, timezone, resolved_format, json_dumps, colors = load_settings()

    logger = loguru.logger
    log_format: str | FormatFunction = resolved_format
    needs_json = False
    needs_logfmt = False
    needs_localtime = False

    if log_format == LoggingFormatType.JSON or (
        isinstance(log_format, str) and log_format.strip() == JSON_FORMAT
    ):
        log_format = _json_formatter
        needs_json = True
    elif log_format == LoggingFormatType.LOGFMT:
        log_format = _logfmt_formatter
        needs_logfmt = True
    elif log_format == LoggingFormatType.PRETTY:
        log_format = _make_pretty_formatter(timezone, colors=colors)
    elif log_format == LoggingFormatType.TEXT:
        log_format = _make_text_formatter(timezone, colors=colors)
    elif isinstance(log_format, str):
        needs_json = "extra[serialized]" in log_format
        needs_logfmt = "extra[logfmt_serialized]" in log_format
        needs_localtime = "extra[localtime]" in log_format

    if (
        needs_localtime
        or needs_json
        or needs_logfmt
        or settings.LOG_OTEL_ENABLED
    ):
        patcher = _LoguruPatcher(
            timezone=timezone,
            enable_localtime=needs_localtime,
            enable_json=needs_json,
            enable_logfmt=needs_logfmt,
            enable_otel=settings.LOG_OTEL_ENABLED,
            json_dumps=json_dumps,
        )
        logger.configure(patcher=patcher)
    else:
        logger.configure()

    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=log_format,
    )
