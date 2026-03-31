"""Shared fixtures for tracing tests."""

import logging
from collections.abc import Generator

import pytest
import structlog
from loguru import logger as loguru_logger


@pytest.fixture
def reset_loguru() -> Generator[None, None, None]:
    """Reset loguru configuration."""
    loguru_logger.configure(handlers=[])
    yield
    loguru_logger.remove()


@pytest.fixture
def reset_structlog() -> Generator[None, None, None]:
    """Reset structlog configuration."""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


@pytest.fixture
def reset_stdlib() -> Generator[None, None, None]:
    """Reset stdlib logging configuration."""
    root = logging.getLogger()
    old_handlers = root.handlers.copy()
    old_level = root.level
    manager = root.manager
    old_logger_levels = {
        name: logger.level
        for name, logger in manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(old_handlers)
    root.setLevel(old_level)
    for name, level in old_logger_levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def reset_backend(
    reset_loguru: None,
    reset_structlog: None,
    reset_stdlib: None,
) -> None:
    """Reset all backends before each test."""
    _ = reset_loguru, reset_structlog, reset_stdlib
