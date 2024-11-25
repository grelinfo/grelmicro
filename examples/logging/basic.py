from grelmicro.logging import configure_logging
from loguru import logger

configure_logging()

logger.debug("This is a debug message")
logger.info("This is an info message")
logger.warning("This is a warning message with context", user="Alice")
logger.error("This is an error message with context", user="Bob")

try:
    raise ValueError("This is an exception message")
except ValueError:
    logger.exception(
        "This is an exception message with context", user="Charlie"
    )
