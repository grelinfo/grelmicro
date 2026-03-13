"""Example: Exception logging with context."""

from loguru import logger

from grelmicro.logging import configure_logging

# Ensure clean state
logger.remove()

configure_logging()

try:
    1 / 0  # noqa: B018
except ZeroDivisionError:
    logger.exception("Operation failed", operation="divide")
