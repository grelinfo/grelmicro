"""Example: LOGFMT format logging."""

from loguru import logger

from grelmicro.log import LogBackendType, LogFormatType, configure

configure(backend=LogBackendType.LOGURU, format=LogFormatType.LOGFMT)

logger.info("Request handled", method="GET", path="/health", status=200)
