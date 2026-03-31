"""Tracing.

Unified instrumentation inspired by Rust's tracing crate.
Creates OTel spans and enriches log records with structured context.
"""

from grelmicro.tracing._context import add_context, get_context
from grelmicro.tracing._instrument import instrument
from grelmicro.tracing._span import span
from grelmicro.tracing.errors import TracingError

__all__ = [
    "TracingError",
    "add_context",
    "get_context",
    "instrument",
    "span",
]
