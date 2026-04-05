"""Health Registry State.

Module-level singleton for the health registry, following the same
pattern as backend registries in sync, cache, and resilience modules.
"""

import warnings

from grelmicro.health._registry import HealthRegistry
from grelmicro.health.errors import HealthError

_state: dict[str, HealthRegistry] = {}


class HealthRegistryNotLoadedError(HealthError, RuntimeError):
    """Raised when the health registry is accessed before being created."""


def set_health_registry(
    registry: HealthRegistry, *, stacklevel: int = 2
) -> None:
    """Register the health registry singleton.

    Called automatically by ``HealthRegistry.__init__`` when
    ``auto_register=True`` (the default).
    """
    if "registry" in _state:
        warnings.warn(
            "Overwriting already-registered health registry.",
            stacklevel=stacklevel,
        )
    _state["registry"] = registry


def get_health_registry() -> HealthRegistry:
    """Return the registered health registry.

    Raises:
        HealthRegistryNotLoadedError: If no registry has been created.
    """
    if "registry" not in _state:
        msg = (
            "No health registry loaded. "
            "Create a HealthRegistry instance first "
            "(e.g. ``HealthRegistry()``)."
        )
        raise HealthRegistryNotLoadedError(msg)
    return _state["registry"]


def reset_health_registry() -> None:
    """Remove the registered health registry (for testing)."""
    _state.pop("registry", None)
