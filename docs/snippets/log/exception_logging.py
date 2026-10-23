"""Example: Exception logging with context."""

from loguru import logger

from grelmicro.log import configure

configure(backend="loguru", format="JSON")

try:
    1 / 0  # noqa: B018
except ZeroDivisionError:
    logger.exception("Operation failed", operation="divide")
