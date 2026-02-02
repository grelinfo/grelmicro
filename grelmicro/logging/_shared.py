"""Shared logging utilities."""

from collections.abc import Mapping
from typing import Any

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover
    trace: Any = None

try:
    import orjson

    def json_dumps(obj: Mapping[str, Any]) -> str:
        """Serialize object to JSON string using orjson."""
        return orjson.dumps(obj).decode("utf-8")

except ImportError:  # pragma: no cover
    import json

    def json_dumps(obj: Mapping[str, Any]) -> str:
        """Serialize object to JSON string using stdlib json."""
        return json.dumps(obj, separators=(",", ":"))


def has_opentelemetry() -> bool:
    """Check if OpenTelemetry is available."""
    return trace is not None


def get_otel_trace_context() -> dict[str, str]:
    """Extract OpenTelemetry trace context from current span.

    Returns:
        Dictionary with trace_id and span_id if active span exists,
        otherwise empty dictionary.
    """
    if not trace:
        return {}

    span = trace.get_current_span()
    span_context = span.get_span_context()

    if not span_context.is_valid:
        return {}

    return {
        "trace_id": format(span_context.trace_id, "032x"),
        "span_id": format(span_context.span_id, "016x"),
    }
