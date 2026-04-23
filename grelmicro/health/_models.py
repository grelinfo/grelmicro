"""Health Check Models."""

from enum import StrEnum
from typing import TypedDict

from grelmicro.health._types import HealthDetails


class HealthStatus(StrEnum):
    """Binary health status for a component or aggregate report.

    - ``OK``: the check passed. At the aggregate level: every
      critical check passed (non-critical failures do not flip the
      aggregate).
    - ``ERROR``: the check failed. At the aggregate level: at least
      one critical check failed.
    """

    OK = "ok"
    ERROR = "error"


class CheckResult(TypedDict):
    """Result of a single health check."""

    status: HealthStatus
    critical: bool
    error: str | None
    details: HealthDetails | None


class HealthReport(TypedDict):
    """Aggregated health report across all registered checks."""

    status: HealthStatus
    checks: dict[str, CheckResult]
