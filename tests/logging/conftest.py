"""Shared test helpers for logging tests."""

import json
import logging
from collections.abc import Generator
from typing import Any

import pytest
import structlog
from loguru import logger as loguru_logger

BACKENDS = ["loguru", "structlog", "stdlib"]


def parse_json_log(output: str) -> dict[str, Any]:
    """Parse JSON log output."""
    return json.loads(output.strip())


def parse_json_logs(output: str) -> list[dict[str, Any]]:
    """Parse multi-line JSON log output."""
    return [json.loads(line) for line in output.strip().splitlines() if line]


def log_message(backend: str, msg: str, **kwargs: object) -> None:
    """Log a message using the appropriate backend."""
    if backend == "loguru":
        loguru_logger.info(msg, **kwargs)
    elif backend == "structlog":
        log = structlog.get_logger()
        log.info(msg, **kwargs)
    else:
        stdlib_logger = logging.getLogger(__name__)
        stdlib_logger.info(msg, extra=kwargs)


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
    """Reset stdlib logging configuration (root and child loggers)."""
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
