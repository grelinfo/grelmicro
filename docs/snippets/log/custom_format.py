"""Example: Custom format logging."""

from loguru import logger

from grelmicro.log import configure

configure(backend="loguru", format="{level} | {message}")

logger.info("Custom format example")
