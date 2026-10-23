"""Health Checks.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.health._checks import HealthChecks, HealthChecksConfig
    from grelmicro.health._endpoints import health_asgi
    from grelmicro.health._liveness import Liveness
    from grelmicro.health._models import (
        CheckResult,
        HealthReport,
        HealthStatus,
    )
    from grelmicro.health._types import HealthCheckFunc, HealthDetails
    from grelmicro.health.errors import HealthError

__all__ = [
    "CheckResult",
    "HealthCheckFunc",
    "HealthChecks",
    "HealthChecksConfig",
    "HealthDetails",
    "HealthError",
    "HealthReport",
    "HealthStatus",
    "Liveness",
    "health_asgi",
]

_LAZY: dict[str, str] = {
    "CheckResult": "grelmicro.health._models",
    "HealthCheckFunc": "grelmicro.health._types",
    "HealthChecks": "grelmicro.health._checks",
    "HealthChecksConfig": "grelmicro.health._checks",
    "HealthDetails": "grelmicro.health._types",
    "HealthError": "grelmicro.health.errors",
    "HealthReport": "grelmicro.health._models",
    "HealthStatus": "grelmicro.health._models",
    "Liveness": "grelmicro.health._liveness",
    "health_asgi": "grelmicro.health._endpoints",
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
