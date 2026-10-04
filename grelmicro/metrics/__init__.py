"""Metrics.

OpenTelemetry metrics for grelmicro. Installs a `MeterProvider` for the
app's lifetime, emits per-component metrics from the existing hot paths,
and exposes a `@measure` decorator plus a Prometheus `/metrics` endpoint.
"""

from grelmicro.metrics._component import Metrics
from grelmicro.metrics._endpoints import metrics_asgi
from grelmicro.metrics._measure import measure
from grelmicro.metrics.config import (
    MetricsConfig,
    MetricsExporterType,
)
from grelmicro.metrics.errors import (
    MetricsError,
)

__all__ = [
    "Metrics",
    "MetricsConfig",
    "MetricsError",
    "MetricsExporterType",
    "measure",
    "metrics_asgi",
]
