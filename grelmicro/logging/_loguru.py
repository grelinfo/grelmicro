"""Loguru Logging Backend."""

import sys
from datetime import UTC, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from grelmicro.errors import DependencyNotFoundError
from grelmicro.logging._shared import (
    get_otel_trace_context,
    has_opentelemetry,
    json_dumps,
)
from grelmicro.logging.config import LoggingFormatType, LoggingSettings
from grelmicro.logging.errors import LoggingSettingsValidationError
from grelmicro.logging.types import JSONRecordDict

try:
    import loguru
except ImportError as exc:  # pragma: no cover
    msg = "loguru is required for the loguru logging backend"
    raise ImportError(msg) from exc

if TYPE_CHECKING:
    from loguru import FormatFunction, Record


JSON_FORMAT = "{extra[serialized]}"
TEXT_FORMAT = (
    "<green>{extra[localtime]}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - {message}"
)


class _LoguruPatcher:
    """Internal patcher for enriching loguru records."""

    def __init__(
        self,
        *,
        timezone: tzinfo | None = None,
        enable_localtime: bool = False,
        enable_json: bool = False,
        enable_otel: bool = False,
    ) -> None:
        self.timezone: tzinfo = timezone or UTC
        self.enable_localtime = enable_localtime
        self.enable_json = enable_json
        self.enable_otel = enable_otel

    def __call__(self, record: "Record") -> None:
        if self.enable_otel:
            _otel_patcher(record)
        if self.enable_localtime:
            _localtime_patcher(record, timezone=self.timezone)
        if self.enable_json:
            _json_patcher(record, timezone=self.timezone)


def _json_patcher(record: "Record", *, timezone: tzinfo | None = None) -> None:
    """Patch the record with JSON serialization."""
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

    record["extra"]["serialized"] = json_dumps(json_record)


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


def _json_formatter(record: "Record", timezone: tzinfo | None = None) -> str:
    """Format log record as JSON.

    Note: This is a format function (not a string) to suppress loguru's automatic
    traceback appending. Exceptions are captured in the `ctx` field instead.
    """
    if "serialized" not in record["extra"]:
        _json_patcher(record, timezone=timezone)
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
        DependencyNotFoundError: If OpenTelemetry is enabled but not installed.
        LoggingSettingsValidationError: If environment variables are invalid.
    """
    try:
        settings = LoggingSettings()
    except ValidationError as error:
        raise LoggingSettingsValidationError(error) from None

    if settings.LOG_OTEL_ENABLED and not has_opentelemetry():
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
        log_format = _json_formatter
        needs_json = True
    elif log_format == LoggingFormatType.TEXT:
        log_format = TEXT_FORMAT
        needs_localtime = True

    elif isinstance(log_format, str):
        needs_json = "extra[serialized]" in log_format
        needs_localtime = "extra[localtime]" in log_format

    if needs_localtime or needs_json or settings.LOG_OTEL_ENABLED:
        patcher = _LoguruPatcher(
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
