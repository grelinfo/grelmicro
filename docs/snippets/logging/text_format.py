"""Example: TEXT format logging with timezone."""

from loguru import logger

from grelmicro.logging import configure_logging

# Ensure clean state
logger.remove()

configure_logging()

logger.info("Application started", version="1.0.0")
