from loguru import logger

from grelmicro.log import configure

configure()

logger.debug("This is a debug message")
logger.info("This is an info message")
logger.warning("This is a warning message with context", user="Alice")
logger.error("This is an error message with context", user="Bob")

try:
    raise ValueError("This is an exception message")  # noqa: EM101, TRY003, TRY301
except ValueError:
    logger.exception(
        "This is an exception message with context", user="Charlie"
    )
