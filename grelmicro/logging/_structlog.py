"""Structlog Logging Backend."""

import logging
import sys
import threading
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from grelmicro.errors import DependencyNotFoundError
from grelmicro.logging._shared import (
    get_otel_trace_context,
    has_opentelemetry,
)

try:
    import orjson

    def _orjson_dumps(
        obj: dict[str, Any],
        **_kwargs: object,
    ) -> bytes:
        """Serialize to JSON bytes using orjson."""
        return orjson.dumps(obj)

except ImportError:  # pragma: no cover
    _orjson_dumps = None  # type: ignore[assignment]
from grelmicro.logging.config import LoggingFormatType, LoggingSettings
from grelmicro.logging.errors import LoggingSettingsValidationError
from grelmicro.logging.types import JSONRecordDict

try:
    import structlog
    from structlog.types import EventDict, Processor, WrappedLogger
except ImportError as exc:  # pragma: no cover
    msg = "structlog is required for the structlog logging backend"
    raise ImportError(msg) from exc


def _add_timestamp(
    timezone: tzinfo,
) -> Processor:
    """Create a processor that adds ISO 8601 timestamp."""

    def processor(
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        event_dict["time"] = datetime.now(UTC).astimezone(timezone).isoformat()
        return event_dict

    return processor


def _add_level(
    _logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Add log level to event dict."""
    event_dict["level"] = method_name.upper()
    return event_dict


def _add_thread_name(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Add thread name to event dict."""
    event_dict["thread"] = threading.current_thread().name
    return event_dict


def _add_logger_info(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Add logger info in module:function:line format."""
    # structlog doesn't have built-in call site info, so we extract it
    record = event_dict.get("_record")
    if record:
        # If using stdlib integration, we have the record
        event_dict["logger"] = (
            f"{record.name}:{record.funcName}:{record.lineno}"
        )
    else:
        # For native structlog, we need to get the call site info
        # This is populated by structlog.processors.CallsiteParameterAdder
        # Keys are: module, func_name, lineno
        module = event_dict.pop("module", None)
        func = event_dict.pop("func_name", None)
        lineno = event_dict.pop("lineno", None)
        if module and func and lineno:
            event_dict["logger"] = f"{module}:{func}:{lineno}"
        else:
            event_dict["logger"] = None
    return event_dict


def _add_otel_context(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Add OpenTelemetry trace context if available."""
    trace_context = get_otel_trace_context()
    if trace_context:
        event_dict["trace_id"] = trace_context["trace_id"]
        event_dict["span_id"] = trace_context["span_id"]
    return event_dict


def _build_json_record(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> JSONRecordDict:
    """Build and validate JSONRecordDict from event dict."""
    # Reserved keys that are part of JSONRecordDict structure
    reserved_keys = {
        "time",
        "level",
        "thread",
        "logger",
        "event",
        "trace_id",
        "span_id",
        "_record",
    }

    # Extract user context (non-reserved keys)
    ctx = {k: v for k, v in event_dict.items() if k not in reserved_keys}

    # Build the record with required fields
    json_record = JSONRecordDict(
        time=event_dict["time"],
        level=event_dict["level"],
        thread=event_dict["thread"],
        logger=event_dict["logger"],
        msg=event_dict.get("event", ""),
    )

    # Add optional trace fields
    if "trace_id" in event_dict:
        json_record["trace_id"] = event_dict["trace_id"]
    if "span_id" in event_dict:
        json_record["span_id"] = event_dict["span_id"]

    # Add context if present
    if ctx:
        json_record["ctx"] = ctx

    return json_record


def configure_logging() -> None:
    """Configure logging with structlog.

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

    timezone = ZoneInfo(str(settings.LOG_TIMEZONE))
    use_json = settings.LOG_FORMAT == LoggingFormatType.JSON

    # Build processor chain
    processors: list[Processor] = [
        # Add call site info for native structlog
        structlog.processors.CallsiteParameterAdder(
            [
                structlog.processors.CallsiteParameter.MODULE,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            ],
            additional_ignores=["grelmicro.logging"],
        ),
        # Merge bound context
        structlog.contextvars.merge_contextvars,
        # Add standard fields
        _add_timestamp(timezone),
        _add_level,
        _add_thread_name,
        _add_logger_info,
    ]

    # Add OTel context if enabled
    if settings.LOG_OTEL_ENABLED:
        processors.append(_add_otel_context)

    # Format-specific processors
    if use_json:
        processors.append(_build_json_record)
        # Use orjson if available for better performance
        if _orjson_dumps:
            processors.append(
                structlog.processors.JSONRenderer(serializer=_orjson_dumps)
            )
            logger_factory = structlog.BytesLoggerFactory(file=sys.stdout.buffer)
        else:
            processors.append(structlog.processors.JSONRenderer())
            logger_factory = structlog.PrintLoggerFactory(file=sys.stdout)
    else:
        # TEXT format or custom format - use ConsoleRenderer
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=sys.stdout.isatty(),
            )
        )
        logger_factory = structlog.PrintLoggerFactory(file=sys.stdout)

    # Map log level string to logging module integer
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=logger_factory,
        cache_logger_on_first_use=True,
    )
