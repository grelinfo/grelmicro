"""Guarded OpenTelemetry trace propagation for the outbox.

`publish` injects the current trace context into a message's headers, and the
relay starts a consumer span linked to it on delivery, following the messaging
semantic conventions. Every helper is a no-op when the `opentelemetry` package
is absent, resolved once and cached so the hot path stays cheap.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from functools import cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID


@cache
def _otel() -> Any:  # noqa: ANN401
    """Return `(propagate, trace, tracer)`, or None when OTel is absent.

    The tracer follows the global provider, so a span reaches the provider
    installed when it starts.
    """
    try:
        from opentelemetry import propagate, trace  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return None
    from grelmicro.trace._otel import GlobalTracer  # noqa: PLC0415

    return propagate, trace, GlobalTracer(trace, "grelmicro.outbox")


def inject_trace_context(headers: dict[str, Any]) -> None:
    """Inject the current trace context into `headers` (W3C `traceparent`).

    A no-op when OpenTelemetry is not installed or no span is active.
    """
    handles = _otel()
    if handles is None:
        return
    handles[0].inject(headers)


@contextmanager
def consumer_span(
    *, topic: str, message_id: UUID, headers: Mapping[str, Any]
) -> Generator[None]:
    """Run the handler inside a consumer span linked to the publisher.

    A no-op when OpenTelemetry is not installed. The span uses the message's
    `traceparent` header as its parent, so the delivery links to the request
    that staged the message. The span records handler exceptions and sets its
    status on its own, so nothing extra is done here.
    """
    handles = _otel()
    if handles is None:
        yield
        return
    propagate, trace, tracer = handles
    context = propagate.extract(headers)
    with tracer.get().start_as_current_span(
        f"process {topic}",
        context=context,
        kind=trace.SpanKind.CONSUMER,
        attributes={
            "messaging.system": "grelmicro_outbox",
            "messaging.destination.name": topic,
            "messaging.operation": "process",
            "messaging.message.id": str(message_id),
        },
    ):
        yield
