"""Logging types."""

from typing import NotRequired

from typing_extensions import TypedDict


class ErrorDict(TypedDict):
    """Structured error representation."""

    type: str
    message: str
    stack: NotRequired[str]


class JSONRecordDict(TypedDict):
    """Structured JSON log record.

    Core fields follow industry conventions (slog, zap, zerolog).
    Extra context fields are merged flat at the top level.

    Example::

        {"time": "2026-03-30T14:00:00+00:00", "level": "INFO",
         "msg": "request handled", "caller": "api:handle:45",
         "method": "GET", "status": 200}
    """

    time: str
    level: str
    msg: str
    caller: str
    trace_id: NotRequired[str]
    span_id: NotRequired[str]
    error: NotRequired[ErrorDict]
