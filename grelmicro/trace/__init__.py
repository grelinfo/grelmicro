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
        TraceExporter,
        TraceProcessor,
        TraceSampler,
    )
    from grelmicro.trace.config import (
        TraceExporterType as TraceExporterType,
    )
    from grelmicro.trace.config import (
        TraceProcessorType as TraceProcessorType,
    )
    from grelmicro.trace.config import (
        TraceSamplerType as TraceSamplerType,
    )
    from grelmicro.trace.errors import TraceError

__all__ = [
    "Trace",
    "TraceConfig",
    "TraceError",
    "TraceExporter",
    "TraceExporterType",
    "TraceProcessor",
    "TraceProcessorType",
    "TraceSampler",
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
    "TraceExporter": "grelmicro.trace.config",
    "TraceProcessor": "grelmicro.trace.config",
    "TraceSampler": "grelmicro.trace.config",
    "add_context": "grelmicro.trace._context",
    "get_context": "grelmicro.trace._context",
    "instrument": "grelmicro.trace._instrument",
    "span": "grelmicro.trace._span",
}
"""The submodule each public name loads from."""

_MODE_ENUMS = frozenset(
    {"TraceExporterType", "TraceProcessorType", "TraceSamplerType"}
)
"""The mode enums removed in 0.44.0."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package.

    A mode enum removed in 0.44.0 warns on every access and is not cached.
    """
    module = _LAZY.get(name)
    if module is None and name in _MODE_ENUMS:
        from grelmicro._mode_enums import deprecated_enum  # noqa: PLC0415

        return deprecated_enum(name)
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
