"""Loguru Logging."""

import json
import sys
from collections.abc import Mapping
from datetime import UTC, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from grelmicro.errors import DependencyNotFoundError
from grelmicro.logging.config import LoggingFormatType, LoggingSettings
from grelmicro.logging.errors import LoggingSettingsValidationError
from grelmicro.logging.types import JSONRecordDict

if TYPE_CHECKING:
    from loguru import FormatFunction, Record

try:
    import loguru
except ImportError:  # pragma: no cover
    loguru: Any = None

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover
    trace: Any = None

try:
    import orjson

    def _json_dumps(obj: Mapping[str, Any]) -> str:
        return orjson.dumps(obj).decode("utf-8")
except ImportError:  # pragma: no cover
    import json

    def _json_dumps(obj: Mapping[str, Any]) -> str:
        return json.dumps(obj, separators=(",", ":"))


JSON_FORMAT = "{extra[serialized]}"
"""Default JSON format for structured logging."""

TEXT_FORMAT = (
    "<green>{extra[localtime]}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - {message}"
)
"""Default text human-readable format for logging.

Time is displayed in the timezone specified by `LOG_TIMEZONE`.
"""


class LoguruPatcher:
    """Loguru record patcher for enriching log entries.

    Enriches loguru records with:
    - Localized timestamps (localtime) in the specified timezone
    - Structured JSON serialization (serialized) for structured logging
    - OpenTelemetry trace context (trace_id, span_id) for distributed tracing
    """

    def __init__(
        self,
        *,
        timezone: tzinfo | None = None,
        enable_localtime: bool = False,
        enable_json: bool = False,
        enable_otel: bool = False,
    ) -> None:
        """Initialize the patcher with enrichment configuration.

        Args:
            timezone: Timezone for timestamp conversion (localtime and JSON timestamps).
                Defaults to UTC if not specified.
            enable_localtime: Enable localized timestamp string in record extra.
                Adds 'localtime' field formatted as "YYYY-MM-DD HH:MM:SS.mmm".
            enable_json: Enable JSON serialization of log records.
                Adds 'serialized' field with JSONRecordDict representation.
            enable_otel: Enable OpenTelemetry trace context extraction.
                Adds 'trace_id' and 'span_id' fields when active span exists.
        """
        self.timezone: tzinfo = timezone or UTC
        self.enable_localtime: bool = enable_localtime
        self.enable_json: bool = enable_json
        self.enable_otel: bool = enable_otel

    def __call__(self, record: "Record") -> None:
        """Patch the loguru record with enabled enrichments."""
        if self.enable_otel:
            otel_patcher(record)
        if self.enable_localtime:
            localtime_patcher(record, timezone=self.timezone)
        if self.enable_json:
            json_patcher(record, timezone=self.timezone)


def json_patcher(record: "Record", *, timezone: tzinfo | None = None) -> None:
    """Patch the serialized log record with `JSONRecordDict` representation."""
    json_record = JSONRecordDict(
        time=record["time"].astimezone(timezone or UTC).isoformat(),
        level=record["level"].name,
        thread=record["thread"].name,
        logger=f"{record['name']}:{record['function']}:{record['line']}",
        msg=record["message"],
    )

    # Reserved keys that should not go into ctx
    reserved_keys = {
        "serialized",
        "localtime",
        "trace_id",
        "span_id",
    }

    # Extract trace fields to top level
    if "trace_id" in record["extra"]:
        json_record["trace_id"] = record["extra"]["trace_id"]
    if "span_id" in record["extra"]:
        json_record["span_id"] = record["extra"]["span_id"]

    # Application context goes in ctx (excluding reserved keys)
    ctx = {k: v for k, v in record["extra"].items() if k not in reserved_keys}
    exception = record["exception"]

    if exception and exception.type:
        ctx["exception"] = f"{exception.type.__name__}: {exception.value!s}"

    if ctx:
        json_record["ctx"] = ctx

    record["extra"]["serialized"] = _json_dumps(json_record)


def localtime_patcher(
    record: "Record",
    *,
    timezone: tzinfo | None = None,
) -> None:
    """Patch the log record with localized time with the format "YYYY-MM-DD HH:MM:SS.mmm"."""
    record["extra"]["localtime"] = (
        record["time"]
        .astimezone(timezone or UTC)
        .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    )


def otel_patcher(record: "Record") -> None:
    """Patch the log record with OpenTelemetry trace context.

    Extracts trace_id and span_id from the active OpenTelemetry span.

    If OpenTelemetry is not installed or no active trace exists, this function
    does nothing (no-op).
    """
    if not trace:
        return

    span = trace.get_current_span()
    span_context = span.get_span_context()

    if not span_context.is_valid:
        return

    record["extra"]["trace_id"] = format(span_context.trace_id, "032x")
    record["extra"]["span_id"] = format(span_context.span_id, "016x")


def json_formatter(record: "Record", timezone: tzinfo | None = None) -> str:
    """Format log record with `JSONRecordDict` representation.

    This function does not return the formatted record directly but provides the format to use when
    writing to the sink.

    Note: This is a format function (not a format string) to prevent loguru from automatically
    appending exception tracebacks. When using format strings, loguru appends tracebacks after
    the formatted output. With format functions, tracebacks are suppressed, which is desired for
    JSON logging where exceptions are captured in the `ctx` field.
    """
    if "serialized" not in record["extra"]:
        json_patcher(record, timezone=timezone)
    return JSON_FORMAT + "\n"


def configure_logging() -> None:
    """Configure logging with loguru.

    Simple twelve-factor app logging configuration that logs to stdout.

    Environment Variables:
        LOG_LEVEL: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default: INFO
        LOG_FORMAT: Log format (JSON, TEXT, or custom template). Default: JSON
        LOG_TIMEZONE: IANA timezone for timestamps (e.g., "UTC", "Europe/Zurich"). Default: UTC
        LOG_OTEL_ENABLED: Enable OpenTelemetry trace context extraction.
            Default: True if OpenTelemetry is installed, else False.

    Raises:
        DependencyNotFoundError: If the loguru module is not installed.
        LoggingSettingsValidationError: If environment variables are invalid.
    """
    if not loguru:
        raise DependencyNotFoundError(module="loguru")

    try:
        settings = LoggingSettings()
    except ValidationError as error:
        raise LoggingSettingsValidationError(error) from None

    if settings.LOG_OTEL_ENABLED and not trace:
        raise DependencyNotFoundError(module="opentelemetry")

    logger = loguru.logger
    log_format: str | FormatFunction = settings.LOG_FORMAT
    timezone = ZoneInfo(str(settings.LOG_TIMEZONE))
    needs_json = False
    needs_localtime = False

    if (
        log_format == LoggingFormatType.JSON
        or log_format.strip() == JSON_FORMAT
    ):
        log_format = json_formatter
        needs_json = True
    elif log_format == LoggingFormatType.TEXT:
        log_format = TEXT_FORMAT
        needs_localtime = True

    elif isinstance(log_format, str):
        needs_json = "extra[serialized]" in log_format
        needs_localtime = "extra[localtime]" in log_format

    if needs_localtime or needs_json or settings.LOG_OTEL_ENABLED:
        patcher = LoguruPatcher(
            timezone=timezone,
            enable_localtime=needs_localtime,
            enable_json=needs_json,
            enable_otel=settings.LOG_OTEL_ENABLED,
        )
        logger.configure(patcher=patcher)
    else:
        # No patcher needed
        logger.configure()

    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=log_format,
    )
