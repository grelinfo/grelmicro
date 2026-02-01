"""Logging types."""

from typing import Any, NotRequired

from typing_extensions import TypedDict


class JSONRecordDict(TypedDict):
    """JSON log record representation.

    The time use a ISO 8601 string.
    """

    time: str
    level: str
    msg: str
    logger: str | None
    thread: str
    trace_id: NotRequired[str]
    span_id: NotRequired[str]
    ctx: NotRequired[dict[Any, Any]]
