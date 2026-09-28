"""Example: Exception logging with context."""

from loguru import logger

from grelmicro.log import LogBackendType, LogFormatType, configure

configure(backend=LogBackendType.LOGURU, format=LogFormatType.JSON)

try:
    1 / 0  # noqa: B018
except ZeroDivisionError:
    logger.exception("Operation failed", operation="divide")
