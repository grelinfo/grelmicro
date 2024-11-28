"""Loguru Logging."""

import json
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NotRequired

from pydantic import ValidationError
from typing_extensions import TypedDict

from grelmicro.errors import DependencyNotFoundError
from grelmicro.logging.config import LoggingFormatType, LoggingSettings
from grelmicro.logging.errors import LoggingSettingsValidationError

if TYPE_CHECKING:
    from loguru import FormatFunction, Record

try:
    import loguru
except ImportError:  # pragma: no cover
    loguru = None  # type: ignore[assignment]

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
    ctx: NotRequired[dict[Any, Any]]


def json_patcher(record: "Record") -> None:
    """Patch the serialized log record with `JSONRecordDict` representation."""
    json_record = JSONRecordDict(
        time=record["time"].isoformat(),
        level=record["level"].name,
        thread=record["thread"].name,
        logger=f'{record["name"]}:{record["function"]}:{record["line"]}',
        msg=record["message"],
    )

    ctx = {k: v for k, v in record["extra"].items() if k != "serialized"}
    exception = record["exception"]

    if exception and exception.type:
        ctx["exception"] = f"{exception.type.__name__}: {exception.value!s}"

    if ctx:
        json_record["ctx"] = ctx

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
    - LOG_FORMAT: JSON | TEXT or any loguru template to format logged message (default: JSON).

    Raises:
        MissingDependencyError: If the loguru module is not installed.
        LoggingSettingsError: If the LOG_FORMAT or LOG_LEVEL environment variable is invalid
    """
    if not loguru:
        raise DependencyNotFoundError(module="loguru")

    try:
        settings = LoggingSettings()
    except ValidationError as error:
        raise LoggingSettingsValidationError(error) from None

    logger = loguru.logger
    log_format: str | FormatFunction = settings.LOG_FORMAT

    if log_format is LoggingFormatType.JSON:
        log_format = json_formatter
    elif log_format is LoggingFormatType.TEXT:
        log_format = TEXT_FORMAT

    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=log_format,
    )
