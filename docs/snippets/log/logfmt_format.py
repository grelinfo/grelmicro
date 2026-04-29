"""Example: LOGFMT format logging."""

from loguru import logger

from grelmicro.log import configure

logger.remove()

configure()

logger.info("Request handled", method="GET", path="/health", status=200)
