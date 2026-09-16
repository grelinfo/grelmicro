"""Example: TEXT format logging with timezone."""

from loguru import logger

from grelmicro.log import LogBackendType, LogFormatType, configure

configure(backend=LogBackendType.LOGURU, format=LogFormatType.TEXT)

logger.info("Application started", version="1.0.0")
