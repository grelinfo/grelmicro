"""Health Check Registry."""

from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
from grelmicro.health._backends import get_health_registry, health_registry
from grelmicro.health._models import (
    CheckResult,
    HealthReport,
    HealthStatus,
)
from grelmicro.health._registry import HealthRegistry, HealthRegistryConfig
from grelmicro.health._types import HealthCheckFunc, HealthDetails
from grelmicro.health.errors import HealthError


def register(
    name: Annotated[str, Doc("Name to register the registry under.")],
    registry: Annotated[HealthRegistry, Doc("The health registry instance.")],
) -> None:
    """Register ``registry`` under ``name``."""
    health_registry.register(name, registry)


def unregister(
    name: Annotated[str, Doc("Name of the registered instance to remove.")],
    registry: Annotated[
        HealthRegistry | None,
        Doc("Optional instance for an identity-checked removal."),
    ] = None,
) -> None:
    """Remove the registered instance under ``name``."""
    health_registry.unregister(name, registry)


def use_registry(
    registry: Annotated[
        HealthRegistry,
        Doc("The health registry to install as the global default."),
    ],
) -> None:
    """Register ``registry`` under the ``"default"`` name."""
    health_registry.register(DEFAULT_NAME, registry)


def use(
    registry: Annotated[
        HealthRegistry | None,
        Doc('Override the ``"default"`` slot for the duration of the block.'),
    ] = None,
    /,
    **named: HealthRegistry,
) -> AbstractContextManager[None]:
    """Install task-scoped registry overrides."""
    return health_registry.use(registry, **named)


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
    "register",
    "unregister",
    "use",
    "use_registry",
]
