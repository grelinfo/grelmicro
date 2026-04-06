"""Health Check Models."""

from enum import StrEnum
from typing import Any, TypedDict


class HealthStatus(StrEnum):
    """Health status of a single component."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class OverallStatus(StrEnum):
    """Aggregated health status across all components."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"


class ComponentHealth(TypedDict):
    """Health status of a single component."""

    name: str
    status: HealthStatus
    critical: bool
    error: str | None
    details: dict[str, Any] | None


class HealthReport(TypedDict):
    """Aggregated health report across all components."""

    status: OverallStatus
    components: list[ComponentHealth]
