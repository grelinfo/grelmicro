"""Idempotency.

Stripe-style idempotency keys for safe retries.

`Idempotency("name", ttl=...)` stores a response under a caller-supplied
key. A repeated key within `ttl` replays the stored response without
running the operation again. Use the explicit block form for the most
control, or the `@idempotent` decorator for a function-level shortcut.

Storage rides the cache layer. Pass an explicit `cache=` or leave it
unset to resolve the active app's `Cache` component backend.
"""

from grelmicro.idempotency._decorator import idempotent
from grelmicro.idempotency._idempotency import Idempotency, Operation
from grelmicro.idempotency.config import IdempotencyConfig
from grelmicro.idempotency.errors import (
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencySettingsValidationError,
    IdempotencyStateError,
)

__all__ = [
    "Idempotency",
    "IdempotencyConfig",
    "IdempotencyConflictError",
    "IdempotencyError",
    "IdempotencySettingsValidationError",
    "IdempotencyStateError",
    "Operation",
    "idempotent",
]
