"""Health Check Registry."""

from grelmicro.health._models import (
    ComponentHealth,
    HealthReport,
    HealthStatus,
    OverallStatus,
)
from grelmicro.health._protocol import HealthChecker
from grelmicro.health._registry import HealthRegistry, HealthRegistryConfig
from grelmicro.health._state import (
    HealthRegistryNotLoadedError,
    get_health_registry,
)
from grelmicro.health.errors import HealthCheckTimeoutError, HealthError

__all__ = [
    "ComponentHealth",
    "HealthCheckTimeoutError",
    "HealthChecker",
    "HealthError",
    "HealthRegistry",
    "HealthRegistryConfig",
    "HealthRegistryNotLoadedError",
    "HealthReport",
    "HealthStatus",
    "OverallStatus",
    "get_health_registry",
]
