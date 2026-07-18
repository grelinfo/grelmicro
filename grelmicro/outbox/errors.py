"""Outbox Errors."""

from __future__ import annotations

from grelmicro.errors import GrelmicroError, SettingsValidationError


class OutboxError(GrelmicroError):
    """Base outbox error."""


class OutboxSettingsValidationError(OutboxError, SettingsValidationError):
    """Outbox Settings Validation Error."""


class OutboxTransactionError(OutboxError, RuntimeError):
    """Raised when `publish` gets a handle with no open transaction.

    The message must join the caller's transaction so it commits with the
    business write. A connection or session outside a transaction would
    commit the message on its own, so `publish` refuses it.
    """


class OutboxHandleError(OutboxError, TypeError):
    """Raised when `publish` gets a handle it cannot use.

    A pool or an engine hands out a fresh connection, so the message would
    land in a separate transaction. Pass a connection or session that is
    already inside your transaction.
    """


class HandlerNotFoundError(OutboxError, LookupError):
    """Raised when a topic has no registered handler."""


class HandlerAlreadyRegisteredError(OutboxError, ValueError):
    """Raised when a topic is registered twice."""
