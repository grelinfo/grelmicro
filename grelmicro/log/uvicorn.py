"""Uvicorn-friendly formatters for dictConfig usage."""

from __future__ import annotations

import sys
from copy import copy
from typing import TYPE_CHECKING, Any, cast

from grelmicro.log._queue import QueueWriter, get_stream
from grelmicro.log._shared import (
    as_log_config,
    load_settings,
    logfmt_dumps,
    render_pretty_lines,
    render_text_line,
    resolve_template_format,
    resolve_use_colors,
)
from grelmicro.log._stdlib import _STANDARD_LOG_RECORD_ATTRS, _BaseFormatter
from grelmicro.log.config import LogConfig, LogFormatType

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable, Mapping
    from typing import TextIO

_UVICORN_LOG_RECORD_ATTRS = _STANDARD_LOG_RECORD_ATTRS | {"asctime"}

_MIN_ACCESS_ARGS = 5

_ACCESS_LOGGER = "uvicorn.access"
"""Logger uvicorn writes its access records to."""

_ACCESS_MESSAGE = '%s - "%s %s HTTP/%s" %d'
"""Message template uvicorn logs every access record with."""


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def apply(config: LogConfig) -> None:
    """Take over uvicorn's own loggers to match the application format.

    Uvicorn installs its own handlers with ``propagate`` off, so its records
    never reach the handler `configure()` sets up and the process emits two
    formats. Its handlers are kept, so a custom handler survives, and both
    the formatter and the stream are replaced.

    A handler still on `sys.stdout` or `sys.stderr` is pointed at the
    stream the rest of the process writes to, so a uvicorn record goes
    through the queue `queue_enabled` installs and lands on one file
    descriptor with the application's own records. A handler that already
    writes somewhere else keeps that stream, and so does anything richer
    than a plain `logging.StreamHandler`, a `FileHandler` among them.

    Uvicorn applies its logging config while building `Config`, before it
    imports the application module, so a `configure()` call at import time
    runs afterwards and has handlers to take over. The process that runs
    `--reload` or `--workers` never imports the application, so its own
    lines are outside this: give uvicorn
    [`dict_config()`][grelmicro.log.dict_config] to cover those too.
    """
    import logging as _logging  # noqa: PLC0415

    stream = get_stream()
    for name in _UVICORN_LOGGERS:
        logger = _logging.getLogger(name)
        for handler in logger.handlers:
            handler.setFormatter(
                UvicornAccessFormatter(config)
                if name == _ACCESS_LOGGER
                else UvicornFormatter(config)
            )
            if type(handler) is not _logging.StreamHandler:
                continue
            plain = cast("logging.StreamHandler[TextIO]", handler)
            if (
                plain.stream is sys.stdout
                or plain.stream is sys.stderr
                # A previous pass already moved it. The writer it points at
                # is stopped when logging is reconfigured, so it has to
                # follow to the one that replaced it.
                or isinstance(plain.stream, QueueWriter)
            ):
                plain.setStream(stream)


class _UvicornBaseFormatter(_BaseFormatter):
    """Base uvicorn formatter that filters uvicorn-specific record attributes."""

    _ignored_record_attrs = _UVICORN_LOG_RECORD_ATTRS


class UvicornFormatter(_UvicornBaseFormatter):
    """Format-aware uvicorn formatter compatible with ``logging.config.dictConfig``.

    Reads ``GREL_LOG_FORMAT`` and produces the matching output (AUTO, JSON,
    LOGFMT, TEXT, PRETTY).

    Pass ``config`` to format against an already-resolved ``LogConfig``
    instead of re-reading the environment. ``configure()`` uses that path so
    uvicorn matches settings passed as keyword arguments, which never reach
    the environment.

    Pass ``use_colors`` to override the terminal check. Uvicorn writes it
    into the document when it is started with ``--use-colors`` or
    ``--no-use-colors``, so a formatter named from a ``dictConfig`` has to
    take it.
    """

    def __init__(
        self,
        config: LogConfig | Mapping[str, Any] | None = None,
        *,
        use_colors: bool | None = None,
    ) -> None:
        """Initialize from a resolved config, or from the environment."""
        settings, timezone, resolved_format, json_dumps, colors = load_settings(
            as_log_config(config)
        )
        colors = resolve_use_colors(
            resolved_format, colors=colors, use_colors=use_colors
        )
        super().__init__(
            timezone=timezone,
            caller_enabled=settings.caller_enabled,
            otel_enabled=settings.otel_enabled,
        )

        self._format_record: Callable[[Mapping[str, Any]], str]
        # A loguru template is read as the format it renders, so uvicorn's
        # records do not land in JSON while the rest of the process reads
        # in something else.
        match resolve_template_format(resolved_format):
            case LogFormatType.LOGFMT:
                self._format_record = logfmt_dumps
            case LogFormatType.PRETTY:
                self._format_record = lambda r: render_pretty_lines(
                    r, colors=colors
                )
            case LogFormatType.TEXT:
                self._format_record = lambda r: render_text_line(
                    r, colors=colors
                )
            case _:  # JSON
                self._format_record = json_dumps

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record."""
        return self._format_record(self._record(record))


class UvicornAccessFormatter(UvicornFormatter):
    """Format-aware uvicorn access log formatter.

    Parses uvicorn's access log tuple arguments into structured fields
    (``client_addr``, ``method``, ``full_path``, ``http_version``,
    ``status_code``).
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format access records with split request fields.

        A record is split when it carries the request arguments and either
        uvicorn's access message or uvicorn's access logger. A record that is
        neither is formatted whole, so an application record reaching this
        formatter through a shared handler keeps its message instead of being
        read as a request.

        The split runs on a copy. A record is formatted once per handler and
        stays readable afterwards, so rewriting `msg` and `args` in place
        would hand every later reader the rewritten record: a second handler
        on the same logger, a queue listener, or a test reading `caplog`.
        """
        args = record.args
        if not (
            isinstance(args, tuple)
            and len(args) >= _MIN_ACCESS_ARGS
            and self._is_access(record)
        ):
            return super().format(record)

        client_addr, method, full_path, http_version, status_code, *_ = args
        access = copy(record)
        access.__dict__.update(
            {
                "client_addr": client_addr,
                "method": method,
                "full_path": full_path,
                "http_version": http_version,
                "status_code": status_code,
            }
        )
        access.msg = "%s %s %s"
        access.args = (method, full_path, status_code)

        return super().format(access)

    @staticmethod
    def _is_access(record: logging.LogRecord) -> bool:
        """Return whether `record` reads as one of uvicorn's access records.

        Uvicorn logs every access record with one message, from one logger.
        Either is enough: a renamed logger still carries the message, and a
        reworded message still comes from the access logger. The caller has
        already checked that the arguments carry a request.

        The argument types are deliberately not inspected. Uvicorn owns that
        tuple and may change what it puts in it, and a type check that no
        longer matches would drop the field split for every real access
        record, which is a worse failure than rendering a request line for a
        record someone else logged on uvicorn's own logger.
        """
        return record.msg == _ACCESS_MESSAGE or record.name == _ACCESS_LOGGER
