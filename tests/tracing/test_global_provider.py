"""Spans follow the global tracer provider when the app replaces it."""

from __future__ import annotations

from collections.abc import Callable, Generator
from uuid import uuid7

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from grelmicro.outbox import _otel as outbox_otel
from grelmicro.security import oauth
from grelmicro.trace import instrument


@instrument
def _instrumented() -> None:
    """Run inside the span `@instrument` opens."""


def _outbox_span() -> None:
    with outbox_otel.consumer_span(topic="job", message_id=uuid7(), headers={}):
        pass


def _oauth_span() -> None:
    with oauth._span({}):
        pass


@pytest.fixture
def install() -> Generator[Callable[[], InMemorySpanExporter]]:
    """Return a function that installs a new global provider, and restore it."""
    saved = trace._TRACER_PROVIDER

    def _install() -> InMemorySpanExporter:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace._TRACER_PROVIDER = provider
        return exporter

    try:
        yield _install
    finally:
        trace._TRACER_PROVIDER = saved


@pytest.mark.parametrize(
    "record",
    [_instrumented, _outbox_span, _oauth_span],
    ids=lambda f: f.__name__,
)
def test_span_reaches_the_provider_that_replaced_the_first(
    install: Callable[[], InMemorySpanExporter],
    record: Callable[[], None],
) -> None:
    """A span goes to the current provider, not the one of the first span."""
    # Arrange
    first = install()
    record()
    second = install()

    # Act
    record()

    # Assert
    assert len(first.get_finished_spans()) == 1
    assert len(second.get_finished_spans()) == 1
