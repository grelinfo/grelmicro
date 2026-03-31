"""Shared test helpers for logging tests."""

import json
import logging
from typing import Any

import structlog
from loguru import logger as loguru_logger


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
