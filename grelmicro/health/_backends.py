"""Health Registry Backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro._backends import BackendRegistry

if TYPE_CHECKING:
    from grelmicro.health._registry import HealthRegistry

health_registry: BackendRegistry[HealthRegistry] = BackendRegistry(
    name="health"
)


def get_health_registry() -> HealthRegistry:
    """Get the default health registry.

    Raises:
        BackendNotLoadedError: If no health registry has been registered.
    """
    return health_registry.get()
