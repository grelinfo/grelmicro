"""Outbox.

Run an async handler exactly after your database transaction commits, at
least once. Stage a message inside your own transaction with `publish`, then
a background relay delivers it to a registered `@handler`.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
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
    "OutboxTransactionError",
    "Retry",
]

_LAZY: dict[str, str] = {
    "Cancel": "grelmicro.outbox._control",
    "HandlerAlreadyRegisteredError": "grelmicro.outbox.errors",
    "HandlerNotFoundError": "grelmicro.outbox.errors",
    "Message": "grelmicro.outbox._message",
    "Outbox": "grelmicro.outbox._component",
    "OutboxBackend": "grelmicro.outbox._protocol",
    "OutboxConfig": "grelmicro.outbox._config",
    "OutboxError": "grelmicro.outbox.errors",
    "OutboxHandleError": "grelmicro.outbox.errors",
    "OutboxRecord": "grelmicro.outbox._message",
    "OutboxTransactionError": "grelmicro.outbox.errors",
    "Retry": "grelmicro.outbox._control",
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
