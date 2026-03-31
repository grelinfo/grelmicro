"""Manual span context manager for mid-function instrumentation."""

from __future__ import annotations

import sys
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Any

from grelmicro.tracing._context import _pop_context, _push_context

if TYPE_CHECKING:
    from collections.abc import Generator

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import StatusCode as _StatusCode
except ImportError:  # pragma: no cover
    _otel_trace: Any = None  # type: ignore[no-redef]
    _StatusCode: Any = None  # type: ignore[no-redef,misc]


@contextmanager
def span(name: str, **fields: object) -> Generator[None, None, None]:
    """Create a span that enriches both OTel and logging context.

    Use for mid-function instrumentation when ``@instrument`` is not enough.
    When an exception propagates, the OTel span is marked as ERROR.

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
    with otel_cm as otel_span:
        try:
            yield
        except BaseException:
            if (
                otel_span is not None
                and hasattr(otel_span, "is_recording")
                and otel_span.is_recording()
            ):
                exc = sys.exc_info()[1]
                if exc is not None:
                    otel_span.set_status(_StatusCode.ERROR, str(exc))  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
                    otel_span.record_exception(exc)
            raise
        finally:
            _pop_context(token)
