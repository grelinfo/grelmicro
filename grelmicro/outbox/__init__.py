"""Outbox.

Run an async handler exactly after your database transaction commits, at
least once. Stage a message inside your own transaction with `publish`, then
a background relay delivers it to a registered `@handler`.
"""

from grelmicro.outbox._component import Outbox
from grelmicro.outbox._config import OutboxConfig
from grelmicro.outbox._control import Cancel, Retry
from grelmicro.outbox._message import Message, OutboxRecord
from grelmicro.outbox._protocol import OutboxBackend
from grelmicro.outbox.errors import (
    HandlerAlreadyRegisteredError,
    HandlerNotFoundError,
    OutboxError,
    OutboxHandleError,
    OutboxSettingsValidationError,
    OutboxTransactionError,
)

__all__ = [
    "Cancel",
    "HandlerAlreadyRegisteredError",
    "HandlerNotFoundError",
    "Message",
    "Outbox",
    "OutboxBackend",
    "OutboxConfig",
    "OutboxError",
    "OutboxHandleError",
    "OutboxRecord",
    "OutboxSettingsValidationError",
    "OutboxTransactionError",
    "Retry",
]
