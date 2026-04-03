"""Example: AUTO format logging (default)."""

from loguru import logger

from grelmicro.logging import configure_logging

logger.remove()

# AUTO is the default: TEXT in terminal, JSON when piped.
# No LOG_FORMAT env var needed.
configure_logging()

logger.info("Application started", version="1.0.0")
