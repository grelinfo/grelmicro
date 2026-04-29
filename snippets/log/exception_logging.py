"""Example: Exception logging with context."""

from loguru import logger

from grelmicro.log import configure

# Ensure clean state
logger.remove()

configure()

try:
    1 / 0  # noqa: B018
except ZeroDivisionError:
    logger.exception("Operation failed", operation="divide")
