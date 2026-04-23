"""Health Check Registry."""

from grelmicro.health._backends import get_health_registry, health_registry
from grelmicro.health._models import (
    CheckResult,
    HealthReport,
    HealthStatus,
)
from grelmicro.health._registry import HealthRegistry, HealthRegistryConfig
from grelmicro.health._types import HealthCheckFunc, HealthDetails
from grelmicro.health.errors import HealthCheckTimeoutError, HealthError

__all__ = [
    "CheckResult",
    "HealthCheckFunc",
    "HealthCheckTimeoutError",
    "HealthDetails",
    "HealthError",
    "HealthRegistry",
    "HealthRegistryConfig",
    "HealthReport",
    "HealthStatus",
    "get_health_registry",
    "health_registry",
]
