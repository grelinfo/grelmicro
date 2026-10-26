"""Metrics.

OpenTelemetry metrics for grelmicro. Installs a `MeterProvider` for the
app's lifetime, emits per-component metrics from the existing hot paths,
and exposes a `@measure` decorator plus a Prometheus `/metrics` endpoint.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.metrics._component import Metrics
    from grelmicro.metrics._endpoints import metrics_asgi
    from grelmicro.metrics._measure import measure
    from grelmicro.metrics.config import (
        MetricsConfig,
        MetricsExporter,
    )
    from grelmicro.metrics.errors import (
        MetricsError,
    )

__all__ = [
    "Metrics",
    "MetricsConfig",
    "MetricsError",
    "MetricsExporter",
    "measure",
    "metrics_asgi",
]

_LAZY: dict[str, str] = {
    "Metrics": "grelmicro.metrics._component",
    "MetricsConfig": "grelmicro.metrics.config",
    "MetricsError": "grelmicro.metrics.errors",
    "MetricsExporter": "grelmicro.metrics.config",
    "measure": "grelmicro.metrics._measure",
    "metrics_asgi": "grelmicro.metrics._endpoints",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
