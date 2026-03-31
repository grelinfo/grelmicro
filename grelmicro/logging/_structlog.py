"""Structlog Logging Backend."""

import logging
import sys
import traceback
from datetime import UTC, datetime, tzinfo

from grelmicro._context import merge_context_into as _merge_context_into
from grelmicro.logging._shared import (
    _json_default,
    get_otel_trace_context,
    load_settings,
)
from grelmicro.logging.config import LoggingSerializerType
from grelmicro.logging.types import ErrorDict

try:
    import structlog
    from structlog.types import EventDict, Processor, WrappedLogger
except ImportError as exc:  # pragma: no cover
    msg = "structlog is required for the structlog logging backend"
    raise ImportError(msg) from exc

_STRUCTLOG_INTERNAL_KEYS = frozenset(
    {
        "event",
        "_record",
        "exc_info",
    }
)


def _add_timestamp(
    timezone: tzinfo,
) -> Processor:
    """Create a processor that adds timestamp as a datetime object."""

    def processor(
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        event_dict["time"] = datetime.now(UTC).astimezone(timezone)
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


def _add_caller_info(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Add caller info in module:function:line format."""
    # structlog doesn't have built-in call site info, so we extract it
    record = event_dict.get("_record")
    if record:
        # If using stdlib integration, we have the record
        event_dict["caller"] = (
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
            event_dict["caller"] = f"{module}:{func}:{lineno}"
        else:
            event_dict["caller"] = "unknown"
    return event_dict


def _add_error_info(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Convert exc_info to structured ErrorDict."""
    exc_info = event_dict.pop("exc_info", None)
    if exc_info is True:
        exc_info = sys.exc_info()
    if exc_info and exc_info[0] is not None:
        exc_type, exc_value, exc_tb = exc_info
        error = ErrorDict(
            type=exc_type.__name__,
            message=str(exc_value),
        )
        if exc_tb is not None:
            error["stack"] = "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            )
        event_dict["error"] = error
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
) -> dict[str, object]:
    """Build flat JSON record from event dict."""
    # Tracing context first, then event extras, then core fields (core wins)
    json_record: dict[str, object] = {}
    _merge_context_into(json_record)
    json_record.update(
        {
            k: v
            for k, v in event_dict.items()
            if k not in _STRUCTLOG_INTERNAL_KEYS
        }
    )
    json_record["time"] = event_dict["time"]
    json_record["level"] = event_dict["level"]
    json_record["msg"] = event_dict.get("event", "")
    json_record["caller"] = event_dict["caller"]

    # Add optional trace fields
    if "trace_id" in event_dict:
        json_record["trace_id"] = event_dict["trace_id"]
    if "span_id" in event_dict:
        json_record["span_id"] = event_dict["span_id"]

    # Add error if present
    if "error" in event_dict:
        json_record["error"] = event_dict["error"]

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
    settings, timezone, use_json, _ = load_settings()

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
        _add_caller_info,
        # Convert exc_info to structured ErrorDict
        _add_error_info,
    ]

    # Add OTel context if enabled
    if settings.LOG_OTEL_ENABLED:
        processors.append(_add_otel_context)

    # Format-specific processors
    if use_json:
        processors.append(_build_json_record)
        # Use orjson bytes serialization for better performance when configured
        if settings.LOG_JSON_SERIALIZER == LoggingSerializerType.ORJSON:
            import orjson  # noqa: PLC0415

            # orjson natively handles datetime; no default needed.
            # Non-serializable extras raise orjson.JSONEncodeError (fail loudly).
            processors.append(
                structlog.processors.JSONRenderer(serializer=orjson.dumps)
            )
            logger_factory = structlog.BytesLoggerFactory(
                file=sys.stdout.buffer
            )
        else:
            processors.append(
                structlog.processors.JSONRenderer(default=_json_default)
            )
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
