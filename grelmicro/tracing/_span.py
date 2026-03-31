"""Manual span context manager for mid-function instrumentation."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Any

from grelmicro.tracing._context import _pop_context, _push_context

if TYPE_CHECKING:
    from collections.abc import Generator

try:
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover
    _otel_trace: Any = None  # type: ignore[no-redef]


@contextmanager
def span(name: str, **fields: object) -> Generator[None, None, None]:
    """Create a span that enriches both OTel and logging context.

    Use for mid-function instrumentation when ``@instrument`` is not enough.

    Example::

        @instrument
        async def process_order(order_id: str):
            logger.info("started")  # has order_id

            with span("payment", provider="stripe"):
                logger.info("charging")  # has order_id + provider

            logger.info("done")  # back to order_id only

    Args:
        name: Span name.
        **fields: Structured fields added to both OTel span and log context.
    """
    token = _push_context(dict(fields))
    otel_cm = (
        _otel_trace.get_tracer(__name__).start_as_current_span(
            name, attributes={k: str(v) for k, v in fields.items()}
        )
        if _otel_trace is not None
        else nullcontext()
    )
    with otel_cm:
        try:
            yield
        finally:
            _pop_context(token)
