"""Uvicorn-friendly JSON formatters for dictConfig usage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.logging._shared import load_settings
from grelmicro.logging._stdlib import _STANDARD_LOG_RECORD_ATTRS, _JSONFormatter

if TYPE_CHECKING:
    import logging

_UVICORN_LOG_RECORD_ATTRS = _STANDARD_LOG_RECORD_ATTRS | {
    "asctime",
    "color_message",
}

_MIN_ACCESS_ARGS = 5


class UvicornJSONFormatter(_JSONFormatter):
    """No-arg JSON formatter compatible with ``logging.config.dictConfig``."""

    _ignored_record_attrs = _UVICORN_LOG_RECORD_ATTRS

    def __init__(self) -> None:
        """Initialize with settings from environment variables."""
        settings, timezone, _, json_dumps, _ = load_settings()
        super().__init__(
            timezone=timezone,
            json_dumps=json_dumps,
            otel_enabled=settings.LOG_OTEL_ENABLED,
        )


class UvicornAccessJSONFormatter(UvicornJSONFormatter):
    """JSON formatter that exposes uvicorn access log components."""

    def format(self, record: logging.LogRecord) -> str:
        """Format access records and add split request fields."""
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
