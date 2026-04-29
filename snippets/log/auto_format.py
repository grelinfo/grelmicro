"""Example: AUTO format logging (default)."""

from loguru import logger

from grelmicro.log import configure

logger.remove()

# AUTO is the default: TEXT in terminal, JSON when piped.
# No LOG_FORMAT env var needed.
configure()

logger.info("Application started", version="1.0.0")
