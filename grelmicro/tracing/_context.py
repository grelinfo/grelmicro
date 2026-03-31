"""Shared tracing context that bridges spans and logging."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

try:
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover
    _otel_trace: Any = None  # type: ignore[no-redef]

# Immutable stack of context dicts, one per active span.
_context_stack: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "grelmicro_tracing_context", default=()
)


def _push_context(fields: dict[str, Any]) -> Token[tuple[dict[str, Any], ...]]:
    """Push a new context frame onto the stack."""
    return _context_stack.set((*_context_stack.get(), fields))


def _pop_context(token: Token[tuple[dict[str, Any], ...]]) -> None:
    """Restore the context stack to its previous state."""
    _context_stack.reset(token)


def _merge_context_into(target: dict[str, Any]) -> None:
    """Merge all active span context into target dict (lowest priority).

    Used by logging backends on the hot path. Avoids creating an
    intermediate dict compared to get_context().
    """
    for frame in _context_stack.get():
        target.update(frame)


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

    Updates both the current logging context and the active OTel span
    (if tracing is configured). No-op if called outside a span.

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

    stack[-1].update(fields)

    if _otel_trace is not None:
        span = _otel_trace.get_current_span()
        if span.is_recording():
            for k, v in fields.items():
                span.set_attribute(k, str(v))
