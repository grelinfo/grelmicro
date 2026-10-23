"""Example: LOGFMT format logging."""

from loguru import logger

from grelmicro.log import configure

configure(backend="loguru", format="LOGFMT")

logger.info("Request handled", method="GET", path="/health", status=200)
