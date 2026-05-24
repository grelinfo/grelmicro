"""Manual span context manager for mid-function instrumentation."""

from __future__ import annotations

import sys
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from grelmicro._context import pop_context as _pop_context
from grelmicro._context import push_context as _push_context
from grelmicro.trace._otel import get as _get_otel

if TYPE_CHECKING:
    from collections.abc import Generator


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
    otel = _get_otel()
    token = _push_context(dict(fields))
    otel_cm = (
        otel.trace.get_tracer(__name__).start_as_current_span(
            name, attributes={k: str(v) for k, v in fields.items()}
        )
        if otel.trace is not None
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
                if exc is not None:  # pragma: no branch
                    otel_span.set_status(otel.status_code.ERROR, str(exc))  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
                    otel_span.record_exception(exc)
            raise
        finally:
            _pop_context(token)
