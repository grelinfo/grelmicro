"""Metrics.

OpenTelemetry metrics for grelmicro. Installs a `MeterProvider` for the
app's lifetime, emits per-component metrics from the existing hot paths,
and exposes a `@measure` decorator plus a Prometheus `/metrics` router.
"""

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from grelmicro.metrics.fastapi import metrics_router

__all__ = [
    "Metrics",
    "MetricsConfig",
    "MetricsError",
    "MetricsExporterType",
    "measure",
    "metrics_asgi",
    "metrics_router",
]


def __getattr__(name: str) -> object:
    """Load `metrics_router` on first access."""
    if name == "metrics_router":
        from grelmicro.metrics.fastapi import metrics_router  # noqa: PLC0415

        globals()[name] = metrics_router  # cache for subsequent access
        return metrics_router
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    """Include lazy attributes in `dir()` for tab completion."""
    return sorted({*globals(), *__all__})
