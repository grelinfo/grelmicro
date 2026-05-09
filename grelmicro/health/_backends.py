"""Health Checks Backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro._backends import DEFAULT_NAME, BackendRegistry

if TYPE_CHECKING:
    from grelmicro.health._checks import HealthChecks

health_checks: BackendRegistry[HealthChecks] = BackendRegistry(name="health")


def get_health_checks(name: str = DEFAULT_NAME) -> HealthChecks:
    """Resolve health checks by ``name``.

    Raises:
        BackendNotLoadedError: If no instance resolves.
    """
    return health_checks.get(name)
