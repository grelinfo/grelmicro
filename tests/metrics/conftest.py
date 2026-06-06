"""Shared fixtures for metrics tests.

`metrics_reader` activates a real `Metrics` component backed by an
in-memory reader so tests can assert the exact metrics emitted by the
hub, the emit helpers, `@measure`, and each auto-instrumentation site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from grelmicro.metrics import _hub
from grelmicro.metrics._component import Metrics

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


class MetricsHarness:
    """A live `Metrics` component wired to an in-memory reader.

    `collect()` returns a flat mapping of metric name to a list of
    `(value, attributes)` tuples, one entry per recorded data point.
    """

    def __init__(
        self, component: Metrics, reader: InMemoryMetricReader
    ) -> None:
        """Wire the component and reader."""
        self.component = component
        self.reader = reader

    def collect(self) -> dict[str, list[tuple[float, dict[str, Any]]]]:
        """Collect current metrics into a name -> data points mapping."""
        data = self.reader.get_metrics_data()
        result: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        if data is None:  # pragma: no cover
            return result
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    points = result.setdefault(metric.name, [])
                    for point in metric.data.data_points:
                        value = getattr(point, "value", None)
                        if value is None:  # histogram
                            value = getattr(point, "sum", 0.0)
                        points.append((value, dict(point.attributes or {})))
        return result

    def points(self, name: str) -> list[tuple[float, dict[str, Any]]]:
        """Return recorded data points for `name`, or an empty list."""
        return self.collect().get(name, [])


@pytest.fixture
async def metrics_reader() -> AsyncIterator[MetricsHarness]:
    """Activate a `Metrics` component with an in-memory reader."""
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


@pytest.fixture
def metrics_off() -> Callable[[], None]:
    """Assert that no `Metrics` component is active (the no-op path)."""

    def _check() -> None:
        assert _hub.active() is None

    return _check
