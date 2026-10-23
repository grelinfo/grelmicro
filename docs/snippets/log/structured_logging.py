"""Example: Structured logging with context."""

from loguru import logger

from grelmicro.log import configure

configure(backend="loguru", format="JSON")

logger.info("User logged in", user_id=123, ip_address="192.168.1.1")
