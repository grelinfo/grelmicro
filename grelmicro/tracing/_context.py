"""Shared tracing context that bridges spans and logging."""

from __future__ import annotations

from typing import Any

from grelmicro._context import (
    context_stack as _context_stack,
)

try:
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover
    _otel_trace: Any = None  # type: ignore[no-redef]


def get_context() -> dict[str, Any]:
    """Get merged context from all active spans (bottom to top)."""
    stack = _context_stack.get()
    if not stack:
        return {}
    if len(stack) == 1:
        return stack[0].copy()
    result: dict[str, Any] = {}
    for frame in stack:
        result.update(frame)
    return result


def add_context(**fields: object) -> None:
    """Add fields to the current span's context.

    Creates a new frame snapshot (safe for concurrent async tasks).
    Updates the active OTel span if tracing is configured.
    No-op if called outside a span.

    Example::

        @instrument
        async def process(order_id: str):
            result = charge()
            add_context(payment_id=result.id, status=result.status)
            logger.info("payment done")  # includes payment_id, status
    """
    stack = _context_stack.get()
    if not stack:
        return

    # Replace frame (not mutate) for concurrent task isolation
    new_frame = {**stack[-1], **fields}
    _context_stack.set((*stack[:-1], new_frame))

    if _otel_trace is not None:
        span = _otel_trace.get_current_span()
        if span.is_recording():
            for k, v in fields.items():
                span.set_attribute(k, str(v))


__all__ = [
    "add_context",
    "get_context",
]
