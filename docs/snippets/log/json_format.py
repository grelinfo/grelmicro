"""Example: JSON format logging with timezone."""

from loguru import logger

from grelmicro.log import LogBackendType, LogFormatType, configure

configure(backend=LogBackendType.LOGURU, format=LogFormatType.JSON)

logger.info("Application started", version="1.0.0", environment="production")
