"""Logging types."""

from typing import NotRequired

from typing_extensions import TypedDict


class ErrorDict(TypedDict):
    """Structured error representation."""

    type: str
    message: str
    stack: NotRequired[str]


class JSONRecordDict(TypedDict):
    """Structured JSON log record after serialization.

    Describes the shape of each log line after it is serialized
    to JSON. Internally, formatters use ``datetime`` objects
    for ``time``. The JSON serializer converts them to ISO 8601.

    The core fields follow common structured-logging conventions.
    Extra context fields are added flat at the top level.

    Example::

        {"time": "2026-03-30T14:00:00+00:00", "level": "INFO",
         "msg": "request handled", "logger": "myapp.api",
         "caller": "handle:45",
         "method": "GET", "status": 200}
    """

    time: str
    level: str
    msg: str
    logger: str
    caller: NotRequired[str]
    trace_id: NotRequired[str]
    span_id: NotRequired[str]
    error: NotRequired[ErrorDict]
