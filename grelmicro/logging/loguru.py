"""Loguru Logging."""

import json
import os
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NotRequired

from typing_extensions import TypedDict

from grelmicro.errors import DependencyNotFoundError, EnvValidationError

if TYPE_CHECKING:
    from loguru import FormatFunction, Record

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    logger = None  # type: ignore[assignment]


try:
    import orjson

    def _json_dumps(obj: Mapping[str, Any]) -> str:
        return orjson.dumps(obj).decode("utf-8")
except ImportError:  # pragma: no cover
    import json

    _json_dumps = json.dumps


JSON_FORMAT = "{extra[serialized]}"
TEXT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - {message}"
)


class JSONRecordDict(TypedDict):
    """JSON log record representation.

    The time use a ISO 8601 string.
    """

    time: str
    level: str
    msg: str
    logger: str | None
    thread: str
    context: NotRequired[dict[Any, Any]]


def json_patcher(record: "Record") -> None:
    """Patch the serialized log record with `JSONRecordDict` representation."""
    json_record = JSONRecordDict(
        time=record["time"].isoformat(),
        level=record["level"].name,
        thread=record["thread"].name,
        logger=f'{record["name"]}:{record["function"]}:{record["line"]}',
        msg=record["message"],
    )
    context = {k: v for k, v in record["extra"].items() if k != "serialized"}
    if context:
        json_record["context"] = context

    record["extra"]["serialized"] = _json_dumps(json_record)


def json_formatter(record: "Record") -> str:
    """Format log record with `JSONRecordDict` representation.

    This function does not return the formatted record directly but provides the format to use when
    writing to the sink.
    """
    json_patcher(record)
    return JSON_FORMAT + "\n"


def configure_logging() -> None:
    """Configure logging with loguru.

    Simple twelve-factor app logging configuration that logs to stdout.

    The following environment variables are used:
    - LOG_LEVEL: The log level to use (default: INFO).
    - LOG_FORMAT: json | text or any loguru template to format logged message (default: json).

    Raises:
        MissingDependencyError: If the loguru module is not installed.
        ValueError: If the LOG_FORMAT or LOG_LEVEL environment variable is invalid
    """
    if not logger:
        msg = "loguru"
        raise DependencyNotFoundError(msg)

    log_format: str | FormatFunction = os.getenv("LOG_FORMAT", "json")

    if isinstance(log_format, str):
        log_format = log_format.lower()
        if log_format == "json":
            log_format = json_formatter
        elif log_format == "text":
            log_format = TEXT_FORMAT

    logger.remove()
    try:
        logger.add(
            sys.stdout, level=os.getenv("LOG_LEVEL", "INFO").upper(), format=log_format
        )
    except ValueError as error:
        if "Level" in str(error):
            env = "LOG_LEVEL"
            raise EnvValidationError(env, str(error)) from error
        raise  # pragma: no cover
