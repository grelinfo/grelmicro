"""Health Check Registry."""

from typing import Annotated

from typing_extensions import Doc

from grelmicro.health._backends import get_health_registry, health_registry
from grelmicro.health._models import (
    CheckResult,
    HealthReport,
    HealthStatus,
)
from grelmicro.health._registry import HealthRegistry, HealthRegistryConfig
from grelmicro.health._types import HealthCheckFunc, HealthDetails
from grelmicro.health.errors import HealthError


def use_registry(
    registry: Annotated[
        HealthRegistry,
        Doc("The health registry to install as the global default."),
    ],
) -> None:
    """Register `registry` as the global default health registry.

    Idempotent: re-registering the same instance is a no-op.
    Registering a different instance warns and replaces.
    """
    health_registry.register(registry)


__all__ = [
    "CheckResult",
    "HealthCheckFunc",
    "HealthDetails",
    "HealthError",
    "HealthRegistry",
    "HealthRegistryConfig",
    "HealthReport",
    "HealthStatus",
    "get_health_registry",
    "use_registry",
]
