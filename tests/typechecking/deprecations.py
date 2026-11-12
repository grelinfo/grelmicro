"""Type assertions for the names and arguments removed in 0.45.0.

Each marked line uses something removed, so ty and mypy report it. The
ignore comment is the assertion: both checkers fail on an ignore comment
that matches nothing, so a name or an argument that comes back fails the
check.
"""

import grelmicro.errors
from grelmicro.health import HealthChecks, HealthChecksConfig


def provider_checks() -> None:
    """Pass `provider_checks=`, or neither, with nothing reported."""
    HealthChecks()
    HealthChecks(provider_checks="require")
    HealthChecks.from_config(HealthChecksConfig(), provider_checks="off")


def provider_checks_none() -> None:
    """Pass `provider_checks=None`, removed in 0.45.0."""
    HealthChecks(provider_checks=None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    HealthChecks.from_config(HealthChecksConfig(), provider_checks=None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def env_load_default_warning() -> None:
    """Use `EnvLoadDefaultWarning`, removed in 0.45.0."""
    print(grelmicro.errors.EnvLoadDefaultWarning)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]  # noqa: T201
