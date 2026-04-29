"""Example: Custom format logging."""

from loguru import logger

from grelmicro.log import configure

# Ensure clean state
logger.remove()

configure()

logger.info("Custom format example")
