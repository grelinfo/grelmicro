"""Observability: metrics emitted by the relay and trace propagation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from opentelemetry.sdk.trace import TracerProvider

from grelmicro.outbox import Message, Outbox, _otel
from grelmicro.outbox._uuid import uuid7
from grelmicro.outbox.memory import MemoryOutboxAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.metrics.conftest import MetricsHarness

pytestmark = [pytest.mark.timeout(5)]


def _fast_outbox(*, max_attempts: int = 10) -> Outbox:
    """Build an outbox on the memory backend tuned for fast tests."""
    return Outbox(
        MemoryOutboxAdapter(),
        poll_interval=0.05,
        retry_base=0.02,
        retry_jitter=0,
        max_attempts=max_attempts,
    )


async def _wait(
    predicate: Callable[[], object],
    timeout: float = 2.0,  # noqa: ASYNC109
) -> None:
    """Poll until `predicate()` is truthy or the timeout elapses."""
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def test_delivery_emits_metrics(metrics_reader: MetricsHarness) -> None:
    """A successful delivery records the delivered counter and duration."""
    outbox = _fast_outbox()
    seen: list[object] = []

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:
        seen.append(message)

    async with outbox:
        await outbox.publish(None, "job", {"n": 1})
        await _wait(lambda: seen)
        await _wait(lambda: metrics_reader.points("grelmicro.outbox.delivered"))

    published = metrics_reader.points("grelmicro.outbox.published")
    delivered = metrics_reader.points("grelmicro.outbox.delivered")
    duration = metrics_reader.points("grelmicro.outbox.handler_duration")
    assert published
    assert published[0][1]["topic"] == "job"
    assert delivered
    assert delivered[0][0] == 1.0
    assert duration


async def test_dead_letter_emits_metric(metrics_reader: MetricsHarness) -> None:
    """An exhausted message records the dead-lettered counter."""
    outbox = _fast_outbox(max_attempts=1)
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        msg = "boom"
        raise RuntimeError(msg)

    async with outbox:
        await outbox.publish(None, "job", {})
        await _wait(
            lambda: metrics_reader.points("grelmicro.outbox.dead_lettered")
        )

    assert metrics_reader.points("grelmicro.outbox.dead_lettered")


async def test_publish_injects_trace_context() -> None:
    """`publish` writes a `traceparent` header inside an active span."""
    tracer = TracerProvider().get_tracer("test")

    backend = MemoryOutboxAdapter()
    outbox = Outbox(backend, relay=False)
    async with outbox:
        with tracer.start_as_current_span("request"):
            await outbox.publish(None, "job", {"n": 1})

    (row,) = backend._rows.values()
    assert "traceparent" in row.record.headers


async def test_trace_context_reaches_handler() -> None:
    """The publisher's trace id rides the headers all the way to the handler.

    This is the cross-process link: `publish` injects the context, it
    persists on the message, and the relay hands it to the handler on
    delivery, so a consumer span parents on the original request.
    """
    tracer = TracerProvider().get_tracer("test")
    outbox = _fast_outbox()
    seen: list[Message[object]] = []

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:
        seen.append(message)

    async with outbox:
        with tracer.start_as_current_span("request") as parent:
            trace_id = format(parent.get_span_context().trace_id, "032x")
            await outbox.publish(None, "job", {"n": 1})
        await _wait(lambda: seen)

    assert trace_id in seen[0].headers["traceparent"]


async def test_trace_helpers_are_noop_without_otel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trace helpers do nothing when OpenTelemetry is unavailable."""
    monkeypatch.setattr(_otel, "_otel", lambda: None)
    headers: dict[str, object] = {}
    _otel.inject_trace_context(headers)
    assert headers == {}
    with _otel.consumer_span(topic="job", message_id=uuid7(), headers={}):
        pass
