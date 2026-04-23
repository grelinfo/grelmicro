"""Health Registry Backend."""

from grelmicro._backends import BackendRegistry
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
