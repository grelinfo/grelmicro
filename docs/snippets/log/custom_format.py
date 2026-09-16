"""Example: Custom format logging."""

from loguru import logger

from grelmicro.log import LogBackendType, configure

configure(backend=LogBackendType.LOGURU, format="{level} | {message}")

logger.info("Custom format example")
