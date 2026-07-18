"""Shared fixtures for outbox tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from grelmicro.metrics import _hub
from grelmicro.metrics._component import Metrics
from tests.metrics.conftest import MetricsHarness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def metrics_reader() -> AsyncIterator[MetricsHarness]:
    """Activate a `Metrics` component backed by an in-memory reader."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    component = Metrics()
    component._provider = provider
    component._resolved = component._explicit_config
    _hub.activate(component)
    try:
        yield MetricsHarness(component, reader)
    finally:
        _hub.deactivate(component)
        provider.shutdown()
