"""Health Checks."""

from contextlib import AbstractContextManager
from typing import Annotated

from typing_extensions import Doc

from grelmicro._backends import DEFAULT_NAME
from grelmicro._deprecation import warn_legacy
from grelmicro.health._backends import get_health_checks, health_checks
from grelmicro.health._checks import HealthChecks, HealthChecksConfig
from grelmicro.health._models import (
    CheckResult,
    HealthReport,
    HealthStatus,
)
from grelmicro.health._types import HealthCheckFunc, HealthDetails
from grelmicro.health.errors import HealthError


def register(
    registry: Annotated[HealthChecks, Doc("The health checks instance.")],
    name: Annotated[
        str, Doc("Name to register the instance under.")
    ] = DEFAULT_NAME,
) -> None:
    """Register ``registry`` under ``name`` (defaults to ``"default"``).

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `Grelmicro(uses=[HealthChecks(...)])` (or `micro.use(HealthChecks(...))`)
    instead.
    """
    warn_legacy(
        "grelmicro.health.register",
        "`Grelmicro(uses=[HealthChecks(...)])`",
    )
    health_checks.register(registry, name)


def unregister(
    name: Annotated[
        str, Doc("Name of the registered instance to remove.")
    ] = DEFAULT_NAME,
    registry: Annotated[
        HealthChecks | None,
        Doc("Optional instance for an identity-checked removal."),
    ] = None,
) -> None:
    """Remove the registered instance under ``name``.

    Deprecated since 0.23.0, removed in 1.0.0. Construct a fresh `Grelmicro`
    app instead of mutating a shared instance.
    """
    warn_legacy(
        "grelmicro.health.unregister",
        "a fresh `Grelmicro(uses=[...])`",
    )
    health_checks.unregister(name, registry)


def use_registry(
    registry: Annotated[
        HealthChecks,
        Doc("The health checks to install as the global default."),
    ],
) -> None:
    """Register ``registry`` under the ``"default"`` name.

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `Grelmicro(uses=[HealthChecks(...)])` instead.
    """
    warn_legacy(
        "grelmicro.health.use_registry",
        "`Grelmicro(uses=[HealthChecks(...)])`",
    )
    health_checks.register(registry, DEFAULT_NAME)


def use(
    registry: Annotated[
        HealthChecks | None,
        Doc('Override the ``"default"`` slot for the duration of the block.'),
    ] = None,
    /,
    **named: HealthChecks,
) -> AbstractContextManager[None]:
    """Install task-scoped overrides.

    Deprecated since 0.23.0, removed in 1.0.0. Use
    `async with micro.override(...)` on the active app instead.
    """
    warn_legacy(
        "grelmicro.health.use",
        "`async with micro.override(...)`",
    )
    return health_checks.use(registry, **named)


__all__ = [
    "CheckResult",
    "HealthCheckFunc",
    "HealthChecks",
    "HealthChecksConfig",
    "HealthDetails",
    "HealthError",
    "HealthReport",
    "HealthStatus",
    "get_health_checks",
    "register",
    "unregister",
    "use",
    "use_registry",
]
