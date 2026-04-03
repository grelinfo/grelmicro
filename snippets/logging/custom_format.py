"""Example: Custom format logging."""

from loguru import logger

from grelmicro.logging import configure_logging

# Ensure clean state
logger.remove()

configure_logging()

logger.info("Custom format example")
