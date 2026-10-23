"""Idempotency.

Stripe-style idempotency keys for safe retries.

`Idempotency("name", ttl=...)` stores a response under a caller-supplied
key. A repeated key within `ttl` replays the stored response without
running the operation again. Use the explicit block form for the most
control, or the `@idempotent` decorator for a function-level shortcut.

Storage rides the cache layer. Pass an explicit `cache=` or leave it
unset to resolve the active app's `Cache` component backend.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.idempotency._decorator import idempotent
    from grelmicro.idempotency._idempotency import Idempotency, Operation
    from grelmicro.idempotency.config import IdempotencyConfig
    from grelmicro.idempotency.errors import (
        IdempotencyConflictError,
        IdempotencyError,
        IdempotencyKeyFunctionError,
        IdempotencyStateError,
        IdempotencyWaitTimeoutError,
    )

__all__ = [
    "Idempotency",
    "IdempotencyConfig",
    "IdempotencyConflictError",
    "IdempotencyError",
    "IdempotencyKeyFunctionError",
    "IdempotencyStateError",
    "IdempotencyWaitTimeoutError",
    "Operation",
    "idempotent",
]

_LAZY: dict[str, str] = {
    "Idempotency": "grelmicro.idempotency._idempotency",
    "IdempotencyConfig": "grelmicro.idempotency.config",
    "IdempotencyConflictError": "grelmicro.idempotency.errors",
    "IdempotencyError": "grelmicro.idempotency.errors",
    "IdempotencyKeyFunctionError": "grelmicro.idempotency.errors",
    "IdempotencyStateError": "grelmicro.idempotency.errors",
    "IdempotencyWaitTimeoutError": "grelmicro.idempotency.errors",
    "Operation": "grelmicro.idempotency._idempotency",
    "idempotent": "grelmicro.idempotency._decorator",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
