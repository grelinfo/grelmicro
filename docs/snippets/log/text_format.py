"""Example: TEXT format logging with timezone."""

from loguru import logger

from grelmicro.log import configure

configure(backend="loguru", format="TEXT")

logger.info("Application started", version="1.0.0")
