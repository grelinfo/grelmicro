"""Type assertions for the names and arguments removed in 0.44.0 and 0.45.0.

Each marked line uses something deprecated, so ty and mypy report it. The
ignore comment is the assertion: both checkers fail on an ignore comment
that matches nothing, so a lost `@deprecated` fails the check.
"""

import grelmicro.errors
from grelmicro import EnvLoadDefaultWarning  # type: ignore[deprecated]  # ty: ignore[deprecated]
from grelmicro.health import HealthChecks, HealthChecksConfig


def provider_checks() -> None:
    """Pass `provider_checks=`, or neither, with nothing reported."""
    HealthChecks()
    HealthChecks(provider_checks="require")
    HealthChecks.from_config(HealthChecksConfig(), provider_checks="off")


def provider_checks_none() -> None:
    """Pass `provider_checks=None`, removed in 0.45.0."""
    HealthChecks(provider_checks=None)  # type: ignore[deprecated]  # ty: ignore[deprecated]
    HealthChecks.from_config(HealthChecksConfig(), provider_checks=None)  # type: ignore[deprecated]  # ty: ignore[deprecated]


def env_load_default_warning() -> None:
    """Use `EnvLoadDefaultWarning`, removed in 0.45.0."""
    print(EnvLoadDefaultWarning.code)  # ty: ignore[deprecated]  # noqa: T201
    print(grelmicro.errors.EnvLoadDefaultWarning)  # type: ignore[deprecated]  # ty: ignore[deprecated]  # noqa: T201
