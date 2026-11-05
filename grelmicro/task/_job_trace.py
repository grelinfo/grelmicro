"""Guarded OpenTelemetry trace link between a job's submitter and its run.

`submit` stores the submitter's trace context with the job. The run opens a
span in a trace of its own, linked to that context, on whichever replica
claims the job. Only the W3C trace context is stored, never baggage, and
every helper is a no-op when the `opentelemetry` package is absent.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import cache
from typing import TYPE_CHECKING, Any, NamedTuple

from grelmicro.trace._otel import exceptions_on_spans

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from opentelemetry.context import Context
    from opentelemetry.trace import Link, SpanKind, StatusCode
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )


class _OTel(NamedTuple):
    """The OpenTelemetry names the helpers use."""

    trace: Any
    context: type[Context]
    link: type[Link]
    consumer: SpanKind
    error: StatusCode
    propagator: TraceContextTextMapPropagator


@cache
def _otel() -> _OTel | None:
    """Return the OpenTelemetry names, or `None` when it is not installed."""
    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.context import Context  # noqa: PLC0415
        from opentelemetry.trace.propagation.tracecontext import (  # noqa: PLC0415
            TraceContextTextMapPropagator,
        )
    except ImportError:  # pragma: no cover
        return None
    return _OTel(
        trace,
        Context,
        trace.Link,
        trace.SpanKind.CONSUMER,
        trace.StatusCode.ERROR,
        TraceContextTextMapPropagator(),
    )


def submitter_context() -> dict[str, str]:
    """Return the current trace context, as text to store with a job.

    Empty when OpenTelemetry is not installed or no span is active.
    """
    otel = _otel()
    carrier: dict[str, str] = {}
    if otel is not None:
        otel.propagator.inject(carrier)
    return carrier


@contextmanager
def run_span(
    name: str, job_id: str, submitter: Mapping[str, str]
) -> Generator[None]:
    """Run one job inside a span of its own trace, linked to `submitter`.

    A span open when the tasks started does not become its parent. Nothing
    is linked when `submitter` holds no valid trace context, since the tracer
    drops such a link. A no-op when OpenTelemetry is not installed.
    """
    otel = _otel()
    if otel is None:
        yield
        return
    linked = otel.trace.get_current_span(
        otel.propagator.extract(submitter, otel.context())
    ).get_span_context()
    with otel.trace.get_tracer("grelmicro.task").start_as_current_span(
        f"job {name}",
        context=otel.context(),
        kind=otel.consumer,
        links=[otel.link(linked)],
        attributes={
            "grelmicro.task.name": name,
            "grelmicro.task.job.id": job_id,
        },
        record_exception=False,
        set_status_on_exception=False,
    ):
        yield


def mark_lost(description: str) -> None:
    """Mark the current span as a job this replica no longer held.

    A no-op when OpenTelemetry is not installed.
    """
    otel = _otel()
    if otel is not None:
        span = otel.trace.get_current_span()
        span.set_attribute("error.type", "job_lost")
        span.set_status(otel.error, description)


def name_error(error: BaseException) -> None:
    """Set `error.type` on the current span, to the type of `error`.

    A no-op when OpenTelemetry is not installed.
    """
    otel = _otel()
    if otel is not None:
        otel.trace.get_current_span().set_attribute(
            "error.type", type(error).__name__
        )


def record_exception(error: BaseException) -> None:
    """Record `error` as an `exception` event on the current span.

    The event holds the message and the traceback. Setting
    `OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN` to `logs` leaves it off. A no-op
    when OpenTelemetry is not installed.
    """
    otel = _otel()
    if otel is not None and exceptions_on_spans():
        otel.trace.get_current_span().record_exception(error)


def mark_failed(description: str) -> None:
    """Mark the current span as failed, saying why.

    A no-op when OpenTelemetry is not installed.
    """
    otel = _otel()
    if otel is not None:
        otel.trace.get_current_span().set_status(otel.error, description)
