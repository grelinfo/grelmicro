"""Example: Structured logging with context."""

from loguru import logger

from grelmicro.logging import configure_logging

# Ensure clean state
logger.remove()

configure_logging()

logger.info("User logged in", user_id=123, ip_address="192.168.1.1")
