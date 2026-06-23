"""Tracing.

Unified instrumentation. Creates OTel spans and enriches log records
with structured context through a single decorator.
"""

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
from grelmicro.trace.errors import TraceError, TraceSettingsValidationError

__all__ = [
    "Trace",
    "TraceConfig",
    "TraceError",
    "TraceExporterType",
    "TraceProcessorType",
    "TraceSamplerType",
    "TraceSettingsValidationError",
    "add_context",
    "get_context",
    "instrument",
    "span",
]
