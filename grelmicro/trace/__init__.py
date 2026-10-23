"""Tracing.

Unified instrumentation. Creates OTel spans and enriches log records
with structured context through a single decorator.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.trace._component import Trace
    from grelmicro.trace._context import add_context, get_context
    from grelmicro.trace._instrument import instrument
    from grelmicro.trace._span import span
    from grelmicro.trace.config import (
        TraceConfig,
        TraceExporterType,
        TraceProcessorType,
        TraceSamplerType,
    )
    from grelmicro.trace.errors import TraceError

__all__ = [
    "Trace",
    "TraceConfig",
    "TraceError",
    "TraceExporterType",
    "TraceProcessorType",
    "TraceSamplerType",
    "add_context",
    "get_context",
    "instrument",
    "span",
]

_LAZY: dict[str, str] = {
    "Trace": "grelmicro.trace._component",
    "TraceConfig": "grelmicro.trace.config",
    "TraceError": "grelmicro.trace.errors",
    "TraceExporterType": "grelmicro.trace.config",
    "TraceProcessorType": "grelmicro.trace.config",
    "TraceSamplerType": "grelmicro.trace.config",
    "add_context": "grelmicro.trace._context",
    "get_context": "grelmicro.trace._context",
    "instrument": "grelmicro.trace._instrument",
    "span": "grelmicro.trace._span",
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
