"""Low-level context bridge shared by logging and tracing.

This module owns the context stack so that neither ``logging`` nor
``tracing`` depends on the other. Both import from here.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

# Immutable stack of immutable context snapshots, one per active span.
# Both the tuple and the dicts are replaced (never mutated) to ensure
# concurrent async tasks sharing a parent context are isolated.
context_stack: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "grelmicro_tracing_context", default=()
)


def push_context(fields: dict[str, Any]) -> Token[tuple[dict[str, Any], ...]]:
    """Push a new context frame onto the stack."""
    return context_stack.set((*context_stack.get(), fields))


def pop_context(token: Token[tuple[dict[str, Any], ...]]) -> None:
    """Restore the context stack to its previous state."""
    context_stack.reset(token)


def merge_context_into(target: dict[str, Any]) -> None:
    """Merge all active span context into target dict.

    Tracing context has lower priority than per-call log extras.
    Used by logging backends on the hot path.
    """
    for frame in context_stack.get():
        target.update(frame)
