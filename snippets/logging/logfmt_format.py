"""Example: LOGFMT format logging."""

from loguru import logger

from grelmicro.logging import configure_logging

logger.remove()

configure_logging()

logger.info("Request handled", method="GET", path="/health", status=200)
