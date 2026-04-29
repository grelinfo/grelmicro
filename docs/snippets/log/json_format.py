"""Example: JSON format logging with timezone."""

from loguru import logger

from grelmicro.log import configure

# Ensure clean state
logger.remove()

configure()

logger.info("Application started", version="1.0.0", environment="production")
