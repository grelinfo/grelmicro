"""Example: AUTO format logging (default)."""

from loguru import logger

from grelmicro.log import LogBackendType, configure

# AUTO is the default: TEXT in a terminal, JSON when the output is piped.
configure(backend=LogBackendType.LOGURU)

logger.info("Application started", version="1.0.0")
