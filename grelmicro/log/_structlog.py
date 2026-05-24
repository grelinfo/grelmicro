"""Structlog Logging Backend."""

import logging
import sys
import traceback
from datetime import UTC, datetime, tzinfo

from grelmicro._context import merge_context_into as _merge_context_into
from grelmicro._json import json_default
from grelmicro.log._shared import (
    get_otel_trace_context,
    load_settings,
    logfmt_dumps,
    render_pretty_lines,
    render_text_line,
)
from grelmicro.log.config import (
    LoggingConfig,
    LoggingFormatType,
    LoggingSerializerType,
)
from grelmicro.log.types import ErrorDict

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


def _add_caller_info(*, caller_enabled: bool = False) -> Processor:
    """Create a processor that adds `logger` and optionally `caller` fields."""

    def processor(
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        record = event_dict.get("_record")
        if record:
            event_dict["logger"] = record.name
            if caller_enabled:
                event_dict["caller"] = f"{record.funcName}:{record.lineno}"
        else:
            module = event_dict.pop("module", None)
            func = event_dict.pop("func_name", None)
            lineno = event_dict.pop("lineno", None)
            if module and func and lineno:
                event_dict["logger"] = module
                if caller_enabled:  # pragma: no branch
                    event_dict["caller"] = f"{func}:{lineno}"
            else:
                event_dict["logger"] = "unknown"
        return event_dict

    return processor


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
        if exc_tb is not None:  # pragma: no branch
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
    if trace_context:  # pragma: no branch
        event_dict["trace_id"] = trace_context["trace_id"]
        event_dict["span_id"] = trace_context["span_id"]
    return event_dict


def _build_flat_record(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> dict[str, object]:
    """Build flat record from event dict.

    Shared by JSON and logfmt renderers.
    """
    flat_record: dict[str, object] = {}
    _merge_context_into(flat_record)
    flat_record.update(
        {
            k: v
            for k, v in event_dict.items()
            if k not in _STRUCTLOG_INTERNAL_KEYS
        }
    )
    flat_record["time"] = event_dict["time"]
    flat_record["level"] = event_dict["level"]
    flat_record["msg"] = event_dict.get("event", "")
    flat_record["logger"] = event_dict["logger"]
    if "caller" in event_dict:
        flat_record["caller"] = event_dict["caller"]

    return flat_record


def _render_logfmt(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> str:
    """Render event dict as logfmt string."""
    flat_record = _build_flat_record(_logger, _method_name, event_dict)
    return logfmt_dumps(flat_record)


def _make_text_renderer(*, colors: bool) -> Processor:
    """Create a processor that renders text format."""

    def processor(
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> str:
        flat = _build_flat_record(_logger, _method_name, event_dict)
        return render_text_line(flat, colors=colors)

    return processor


def _make_pretty_renderer(*, colors: bool) -> Processor:
    """Create a processor that renders pretty multi-line format."""

    def processor(
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> str:
        flat = _build_flat_record(_logger, _method_name, event_dict)
        return render_pretty_lines(flat, colors=colors)

    return processor


def configure(config: LoggingConfig | None = None) -> None:
    """Configure logging with structlog.

    Simple twelve-factor app logging configuration that logs to stdout.

    Environment Variables:
        LOG_LEVEL: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default: INFO
        LOG_FORMAT: Log format (AUTO, JSON, LOGFMT, TEXT, PRETTY). Default: AUTO
        LOG_TIMEZONE: IANA timezone for timestamps (e.g., "UTC", "Europe/Zurich"). Default: UTC
        LOG_CALLER_ENABLED: Include caller (function:line) in log records. Default: False
        LOG_OTEL_ENABLED: Enable OpenTelemetry trace context extraction.
            Default: True if OpenTelemetry is installed, else False.

    Raises:
        DependencyNotFoundError: If OpenTelemetry is enabled but not installed.
        pydantic.ValidationError: If environment variables are invalid.
    """
    settings, timezone, resolved_format, _, colors = load_settings(config)
    caller = settings.caller_enabled

    callsite_params = [structlog.processors.CallsiteParameter.MODULE]
    if caller:
        callsite_params.extend(
            [
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            ]
        )

    processors: list[Processor] = [
        structlog.processors.CallsiteParameterAdder(
            callsite_params,
            additional_ignores=["grelmicro.log"],
        ),
        structlog.contextvars.merge_contextvars,
        _add_timestamp(timezone),
        _add_level,
        _add_caller_info(caller_enabled=caller),
        _add_error_info,
    ]

    if settings.otel_enabled:
        processors.append(_add_otel_context)

    if resolved_format == LoggingFormatType.JSON:
        processors.append(_build_flat_record)
        if settings.json_serializer == LoggingSerializerType.ORJSON:
            import orjson  # noqa: PLC0415

            processors.append(
                structlog.processors.JSONRenderer(serializer=orjson.dumps)
            )
            logger_factory = structlog.BytesLoggerFactory(
                file=sys.stdout.buffer
            )
        else:
            processors.append(
                structlog.processors.JSONRenderer(default=json_default)
            )
            logger_factory = structlog.PrintLoggerFactory(file=sys.stdout)
    elif resolved_format == LoggingFormatType.LOGFMT:
        processors.append(_render_logfmt)
        logger_factory = structlog.PrintLoggerFactory(file=sys.stdout)
    elif resolved_format == LoggingFormatType.PRETTY:
        processors.append(_make_pretty_renderer(colors=colors))
        logger_factory = structlog.PrintLoggerFactory(file=sys.stdout)
    else:
        processors.append(_make_text_renderer(colors=colors))
        logger_factory = structlog.PrintLoggerFactory(file=sys.stdout)

    log_level = getattr(logging, settings.level.upper(), logging.INFO)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=logger_factory,
        cache_logger_on_first_use=True,
    )
