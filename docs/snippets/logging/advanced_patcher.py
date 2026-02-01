"""Example: Advanced LoguruPatcher usage."""

from zoneinfo import ZoneInfo

from loguru import logger

from grelmicro.logging.loguru import JSON_FORMAT, LoguruPatcher

# Create a custom patcher with specific timezone
patcher = LoguruPatcher(
    timezone=ZoneInfo("Europe/Paris"),
    enable_json=True,  # Enable JSON serialization
    enable_localtime=False,  # Disable localtime formatting
)

# Configure logger with the patcher
logger.configure(patcher=patcher)

# Add a sink with JSON format
logger.add("app.log", format=lambda _: JSON_FORMAT + "\n", level="INFO")

logger.info("Custom logging setup", service="api")
