"""Health Registry Backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro._backends import DEFAULT_NAME, BackendRegistry

if TYPE_CHECKING:
    from grelmicro.health._registry import HealthRegistry

health_registry: BackendRegistry[HealthRegistry] = BackendRegistry(
    name="health"
)


def get_health_registry(name: str = DEFAULT_NAME) -> HealthRegistry:
    """Resolve a health registry by ``name``.

    Raises:
        BackendNotLoadedError: If no registry resolves.
    """
    return health_registry.get(name)
