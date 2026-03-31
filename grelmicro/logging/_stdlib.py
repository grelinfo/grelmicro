"""Standard Library Logging Backend."""

import logging
import sys
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any

from grelmicro.logging._shared import (
    get_otel_trace_context,
    load_settings,
)
from grelmicro.logging.types import ErrorDict
from grelmicro.tracing._context import _merge_context_into

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


class _JSONFormatter(logging.Formatter):
    """JSON formatter that produces JSONRecordDict output."""

    def __init__(
        self,
        timezone: tzinfo,
        json_dumps: Callable[[Mapping[str, Any]], str],
        *,
        otel_enabled: bool,
    ) -> None:
        super().__init__()
        self.timezone = timezone
        self.json_dumps = json_dumps
        self.otel_enabled = otel_enabled

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as JSON."""
        # Tracing context first, then log extras, then core fields (core wins)
        json_record: dict[str, Any] = {}
        _merge_context_into(json_record)
        json_record.update(
            {
                k: v
                for k, v in record.__dict__.items()
                if k not in _STANDARD_LOG_RECORD_ATTRS and not callable(v)
            }
        )
        json_record["time"] = datetime.fromtimestamp(
            record.created, tz=UTC
        ).astimezone(self.timezone)
        json_record["level"] = record.levelname
        json_record["msg"] = record.getMessage()
        json_record["caller"] = (
            f"{record.name}:{record.funcName}:{record.lineno}"
        )

        # Add OTel context if enabled
        if self.otel_enabled:
            trace_context = get_otel_trace_context()
            if trace_context:
                json_record["trace_id"] = trace_context["trace_id"]
                json_record["span_id"] = trace_context["span_id"]

        # Handle exception info
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
            json_record["error"] = error

        return self.json_dumps(json_record)


class _TextFormatter(logging.Formatter):
    """Text formatter for human-readable output."""

    def __init__(self, timezone: tzinfo) -> None:
        super().__init__()
        self.timezone = timezone

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as human-readable text."""
        localtime = (
            datetime.fromtimestamp(record.created, tz=UTC)
            .astimezone(self.timezone)
            .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        )
        return (
            f"{localtime} | {record.levelname:<8} | "
            f"{record.name}:{record.funcName}:{record.lineno} - {record.getMessage()}"
        )


def configure_logging() -> None:
    """Configure logging with stdlib.

    Simple twelve-factor app logging configuration that logs to stdout.

    Environment Variables:
        LOG_LEVEL: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default: INFO
        LOG_FORMAT: Log format (JSON or TEXT). Default: JSON
        LOG_TIMEZONE: IANA timezone for timestamps (e.g., "UTC", "Europe/Zurich"). Default: UTC
        LOG_OTEL_ENABLED: Enable OpenTelemetry trace context extraction.
            Default: True if OpenTelemetry is installed, else False.

    Raises:
        DependencyNotFoundError: If OpenTelemetry is enabled but not installed.
        LoggingSettingsValidationError: If environment variables are invalid.
    """
    settings, timezone, use_json, json_dumps = load_settings()

    # Create formatter
    if use_json:
        formatter: logging.Formatter = _JSONFormatter(
            timezone=timezone,
            json_dumps=json_dumps,
            otel_enabled=settings.LOG_OTEL_ENABLED,
        )
    else:
        formatter = _TextFormatter(timezone=timezone)

    # Configure root logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.LOG_LEVEL)
