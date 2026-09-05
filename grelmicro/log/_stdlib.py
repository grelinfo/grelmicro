"""Standard Library Logging Backend."""

import logging
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any

from grelmicro._context import merge_context_into as _merge_context_into
from grelmicro.log._queue import get_stream, get_writer, swap
from grelmicro.log._shared import (
    as_log_config,
    get_otel_trace_context,
    load_settings,
    logfmt_dumps,
    render_pretty_lines,
    render_text_line,
    resolve_template_format,
    resolve_use_colors,
)
from grelmicro.log.config import LogConfig, LogFormatType
from grelmicro.log.types import ErrorDict

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
        # Uvicorn logs an ANSI-colored copy of its message under
        # `color_message`. It renders as an escape sequence in a field, so
        # it is dropped wherever a uvicorn record is read.
        "color_message",
    }
)


def _build_record(
    record: logging.LogRecord,
    timezone: tzinfo,
    *,
    caller_enabled: bool,
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
    log_record["logger"] = record.name
    if caller_enabled:
        log_record["caller"] = f"{record.funcName}:{record.lineno}"

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
        if exc_tb is not None:  # pragma: no branch
            error["stack"] = "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            )
        log_record["error"] = error

    return log_record


class _BaseFormatter(logging.Formatter):
    """Base formatter with shared record building."""

    _ignored_record_attrs: frozenset[str] = _STANDARD_LOG_RECORD_ATTRS

    def __init__(
        self, timezone: tzinfo, *, caller_enabled: bool, otel_enabled: bool
    ) -> None:
        super().__init__()
        self.timezone = timezone
        self.caller_enabled = caller_enabled
        self.otel_enabled = otel_enabled

    def _record(self, record: logging.LogRecord) -> dict[str, Any]:
        # `logging.Formatter.format` sets `record.message` before rendering,
        # and downstream code relies on it: pytest's `caplog` reads it, and so
        # does any handler that formats a record twice. These formatters build
        # their own mapping instead of calling up, so they have to set it too.
        record.message = record.getMessage()
        return _build_record(
            record,
            self.timezone,
            caller_enabled=self.caller_enabled,
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
        caller_enabled: bool,
        otel_enabled: bool,
    ) -> None:
        super().__init__(
            timezone=timezone,
            caller_enabled=caller_enabled,
            otel_enabled=otel_enabled,
        )
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
        self,
        timezone: tzinfo,
        *,
        caller_enabled: bool,
        otel_enabled: bool,
        colors: bool,
    ) -> None:
        super().__init__(
            timezone=timezone,
            caller_enabled=caller_enabled,
            otel_enabled=otel_enabled,
        )
        self.colors = colors

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as human-readable text."""
        return render_text_line(self._record(record), colors=self.colors)


class _PrettyFormatter(_BaseFormatter):
    """Pretty multi-line formatter for verbose debugging."""

    def __init__(
        self,
        timezone: tzinfo,
        *,
        caller_enabled: bool,
        otel_enabled: bool,
        colors: bool,
    ) -> None:
        super().__init__(
            timezone=timezone,
            caller_enabled=caller_enabled,
            otel_enabled=otel_enabled,
        )
        self.colors = colors

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as multi-line pretty output."""
        return render_pretty_lines(self._record(record), colors=self.colors)


def configure(config: LogConfig | None = None) -> None:
    """Configure logging with stdlib.

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
        SettingsValidationError: If environment variables are invalid.
    """
    settings, timezone, resolved_format, json_dumps, colors = load_settings(
        config
    )
    install_root(
        build_formatter(
            resolved_format,
            timezone=timezone,
            json_dumps=json_dumps,
            colors=colors,
            caller_enabled=settings.caller_enabled,
            otel_enabled=settings.otel_enabled,
        ),
        level=settings.level,
    )


def build_formatter(
    resolved_format: LogFormatType | str,
    *,
    timezone: tzinfo,
    json_dumps: Callable[[Mapping[str, Any]], str],
    colors: bool,
    caller_enabled: bool,
    otel_enabled: bool,
) -> logging.Formatter:
    """Return the formatter that renders a record in `resolved_format`.

    Every backend renders standard library records with this one, so a
    record written through `logging` reads the same whichever backend the
    app writes its own records through.
    """
    # A loguru template is read as the format it renders, here rather
    # than at each call site, so no backend can forget to ask.
    resolved_format = resolve_template_format(resolved_format)

    formatter: logging.Formatter
    if resolved_format == LogFormatType.JSON:
        formatter = _JSONFormatter(
            timezone=timezone,
            json_dumps=json_dumps,
            caller_enabled=caller_enabled,
            otel_enabled=otel_enabled,
        )
    elif resolved_format == LogFormatType.LOGFMT:
        formatter = _LogfmtFormatter(
            timezone=timezone,
            caller_enabled=caller_enabled,
            otel_enabled=otel_enabled,
        )
    elif resolved_format == LogFormatType.PRETTY:
        formatter = _PrettyFormatter(
            timezone=timezone,
            caller_enabled=caller_enabled,
            otel_enabled=otel_enabled,
            colors=colors,
        )
    else:
        formatter = _TextFormatter(
            timezone=timezone,
            caller_enabled=caller_enabled,
            otel_enabled=otel_enabled,
            colors=colors,
        )
    return formatter


def formatter(
    config: LogConfig | Mapping[str, Any] | None = None,
    *,
    use_colors: bool | None = None,
) -> logging.Formatter:
    """Return the formatter that renders a record in grelmicro's format.

    Built to be named from a `logging.config.dictConfig`, which is how an
    application server renders its own records the way the application
    does:

    ```python
    {"()": "grelmicro.log.formatter"}
    ```

    [`dict_config()`][grelmicro.log.dict_config] assembles the whole
    document, and this is the piece it names.

    Args:
        config: A resolved `LogConfig`, or the mapping a document carries
            it as. Omit it to read `GREL_LOG_*`.
        use_colors: Whether to colorize, overriding `NO_COLOR`,
            `FORCE_COLOR` and the terminal check. Uvicorn writes it into
            the document when it is started with `--use-colors` or
            `--no-use-colors`.

    Raises:
        DependencyNotFoundError: If orjson or OpenTelemetry is enabled but not installed.
        SettingsValidationError: If configuration is invalid.
    """
    settings, timezone, resolved_format, json_dumps, colors = load_settings(
        as_log_config(config)
    )
    return build_formatter(
        resolved_format,
        timezone=timezone,
        json_dumps=json_dumps,
        colors=resolve_use_colors(
            resolved_format, colors=colors, use_colors=use_colors
        ),
        caller_enabled=settings.caller_enabled,
        otel_enabled=settings.otel_enabled,
    )


def handler(
    config: LogConfig | Mapping[str, Any] | None = None,
) -> logging.Handler:
    """Return the handler grelmicro writes every record through.

    Built to be named from a `logging.config.dictConfig`:

    ```python
    {"()": "grelmicro.log.handler", "formatter": "default"}
    ```

    It writes to the stream the rest of the process writes to, so a record
    a server renders through this document goes through the same queue as
    the application's own. When the settings ask for a queue and no writer
    is running, one is started here, which is what puts a document applied
    on its own behind a queue.

    Args:
        config: A resolved `LogConfig`, or the mapping a document carries
            it as. Omit it to read `GREL_LOG_*`.

    Raises:
        DependencyNotFoundError: If orjson or OpenTelemetry is enabled but not installed.
        SettingsValidationError: If configuration is invalid.
    """
    settings = load_settings(as_log_config(config)).settings
    if settings.queue_enabled and get_writer() is None:
        swap(size=settings.queue_size)
    return logging.StreamHandler(get_stream())


def install_root(formatter: logging.Formatter, *, level: int | str) -> None:
    """Render every standard library record through `formatter`.

    The root logger is where a record ends up when nothing else claimed
    it, which is every record grelmicro's own components write, and every
    record a dependency writes: httpx, SQLAlchemy, redis. Each backend
    installs this, so a service that logs through loguru or structlog
    still reads its dependencies in the format it configured rather than
    watching them fall through to `logging.lastResort`.

    Uvicorn's own records are left where they are. Its default logging
    config gives its loggers their own handlers with propagation off, and
    `grelmicro.log.uvicorn` reformats those in place, so a request line
    renders once. A service that hands uvicorn a `log_config` of its own
    and leaves propagation on is outside that, and would read every
    uvicorn line twice.
    """
    handler = logging.StreamHandler(get_stream())
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Replaced in one assignment rather than cleared and added to. The
    # list is what `callHandlers` walks, and emptying it first leaves a
    # window where a record from another thread reaches no handler at all.
    root_logger.handlers = [handler]
    root_logger.setLevel(level)
