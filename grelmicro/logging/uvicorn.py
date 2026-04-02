"""Uvicorn-friendly formatters for dictConfig usage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.logging._shared import (
    load_settings,
    logfmt_dumps,
    render_pretty_lines,
    render_text_line,
)
from grelmicro.logging._stdlib import _STANDARD_LOG_RECORD_ATTRS, _BaseFormatter
from grelmicro.logging.config import LoggingFormatType

if TYPE_CHECKING:
    import logging

_UVICORN_LOG_RECORD_ATTRS = _STANDARD_LOG_RECORD_ATTRS | {
    "asctime",
    "color_message",
}

_MIN_ACCESS_ARGS = 5


class _UvicornBaseFormatter(_BaseFormatter):
    """Base uvicorn formatter that filters uvicorn-specific record attributes."""

    _ignored_record_attrs = _UVICORN_LOG_RECORD_ATTRS


class UvicornFormatter(_UvicornBaseFormatter):
    """Format-aware uvicorn formatter compatible with ``logging.config.dictConfig``.

    Reads ``LOG_FORMAT`` and produces the matching output (AUTO, JSON, LOGFMT,
    TEXT, PRETTY).  No constructor arguments required.
    """

    def __init__(self) -> None:
        """Initialize with settings from environment variables."""
        settings, timezone, resolved_format, json_dumps, colors = (
            load_settings()
        )
        super().__init__(
            timezone=timezone, otel_enabled=settings.LOG_OTEL_ENABLED
        )

        match resolved_format:
            case LoggingFormatType.LOGFMT:
                self._format_record = logfmt_dumps
            case LoggingFormatType.PRETTY:
                self._format_record = lambda r: render_pretty_lines(
                    r, colors=colors
                )
            case LoggingFormatType.TEXT:
                self._format_record = lambda r: render_text_line(
                    r, colors=colors
                )
            case _:  # JSON and custom strings
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
        """Format access records with split request fields."""
        if (
            isinstance(record.args, tuple)
            and len(record.args) >= _MIN_ACCESS_ARGS
        ):
            client_addr, method, full_path, http_version, status_code, *_ = (
                record.args
            )
            record.__dict__.update(
                {
                    "client_addr": client_addr,
                    "method": method,
                    "full_path": full_path,
                    "http_version": http_version,
                    "status_code": status_code,
                }
            )
            record.msg = "%s %s %s"
            record.args = (method, full_path, status_code)

        return super().format(record)
