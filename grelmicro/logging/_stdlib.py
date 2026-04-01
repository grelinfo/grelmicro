"""Standard Library Logging Backend."""

import logging
import sys
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any

from grelmicro._context import merge_context_into as _merge_context_into
from grelmicro.logging._shared import (
    get_otel_trace_context,
    load_settings,
    logfmt_dumps,
    render_pretty_lines,
    render_text_line,
)
from grelmicro.logging.config import LoggingFormatType
from grelmicro.logging.types import ErrorDict

_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "exc_info",
        "exc_text",
        "thread",
        "threadName",
        "taskName",
        "message",
    }
)


def _build_record(
    record: logging.LogRecord,
    timezone: tzinfo,
    *,
    otel_enabled: bool,
    ignored_attrs: frozenset[str],
) -> dict[str, Any]:
    """Build a structured log record dict from a LogRecord."""
    # Context fields < log extras < core fields (last wins)
    log_record: dict[str, Any] = {}
    _merge_context_into(log_record)
    log_record.update(
        {
            k: v
            for k, v in record.__dict__.items()
            if k not in ignored_attrs
            and not callable(v)
            and not k.startswith("_")
        }
    )
    log_record["time"] = datetime.fromtimestamp(
        record.created, tz=UTC
    ).astimezone(timezone)
    log_record["level"] = record.levelname
    log_record["msg"] = record.getMessage()
    log_record["caller"] = f"{record.name}:{record.funcName}:{record.lineno}"

    if otel_enabled:
        trace_context = get_otel_trace_context()
        if trace_context:
            log_record["trace_id"] = trace_context["trace_id"]
            log_record["span_id"] = trace_context["span_id"]

    if record.exc_info and record.exc_info[0] is not None:
        exc_type, exc_value, exc_tb = record.exc_info
        error = ErrorDict(
            type=exc_type.__name__,
            message=str(exc_value),
        )
        if exc_tb is not None:
            error["stack"] = "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            )
        log_record["error"] = error

    return log_record


class _BaseFormatter(logging.Formatter):
    """Base formatter with shared record building."""

    _ignored_record_attrs: frozenset[str] = _STANDARD_LOG_RECORD_ATTRS

    def __init__(self, timezone: tzinfo, *, otel_enabled: bool) -> None:
        super().__init__()
        self.timezone = timezone
        self.otel_enabled = otel_enabled

    def _record(self, record: logging.LogRecord) -> dict[str, Any]:
        return _build_record(
            record,
            self.timezone,
            otel_enabled=self.otel_enabled,
            ignored_attrs=self._ignored_record_attrs,
        )


class _JSONFormatter(_BaseFormatter):
    """JSON formatter that produces JSONRecordDict output."""

    def __init__(
        self,
        timezone: tzinfo,
        json_dumps: Callable[[Mapping[str, Any]], str],
        *,
        otel_enabled: bool,
    ) -> None:
        super().__init__(timezone=timezone, otel_enabled=otel_enabled)
        self.json_dumps = json_dumps

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as JSON."""
        return self.json_dumps(self._record(record))


class _LogfmtFormatter(_BaseFormatter):
    """Logfmt formatter that produces key=value output."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as logfmt."""
        return logfmt_dumps(self._record(record))


class _TextFormatter(_BaseFormatter):
    """Text formatter for human-readable single-line output with optional colors."""

    def __init__(
        self, timezone: tzinfo, *, otel_enabled: bool, colors: bool
    ) -> None:
        super().__init__(timezone=timezone, otel_enabled=otel_enabled)
        self.colors = colors

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as human-readable text."""
        return render_text_line(self._record(record), colors=self.colors)


class _PrettyFormatter(_BaseFormatter):
    """Pretty multi-line formatter for verbose debugging."""

    def __init__(
        self, timezone: tzinfo, *, otel_enabled: bool, colors: bool
    ) -> None:
        super().__init__(timezone=timezone, otel_enabled=otel_enabled)
        self.colors = colors

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as multi-line pretty output."""
        return render_pretty_lines(self._record(record), colors=self.colors)


def configure_logging() -> None:
    """Configure logging with stdlib.

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
    otel = settings.LOG_OTEL_ENABLED

    formatter: logging.Formatter
    if resolved_format == LoggingFormatType.JSON:
        formatter = _JSONFormatter(
            timezone=timezone,
            json_dumps=json_dumps,
            otel_enabled=otel,
        )
    elif resolved_format == LoggingFormatType.LOGFMT:
        formatter = _LogfmtFormatter(timezone=timezone, otel_enabled=otel)
    elif resolved_format == LoggingFormatType.PRETTY:
        formatter = _PrettyFormatter(
            timezone=timezone, otel_enabled=otel, colors=colors
        )
    else:
        formatter = _TextFormatter(
            timezone=timezone, otel_enabled=otel, colors=colors
        )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.LOG_LEVEL)
