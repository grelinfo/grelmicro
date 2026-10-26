"""Type assertions for the arguments removed in 0.44.0.

Each marked line uses something deprecated, so ty and mypy report it. The
ignore comment is the assertion: both checkers fail on an ignore comment
that matches nothing, so a lost `@deprecated` fails the check.
"""

from grelmicro.health import HealthChecks, HealthChecksConfig


def auto_health() -> None:
    """Pass `auto_health=`, renamed to `provider_checks=`."""
    HealthChecks(auto_health=True)  # type: ignore[deprecated]  # ty: ignore[deprecated]
    HealthChecks.from_config(HealthChecksConfig(), auto_health=False)  # type: ignore[deprecated]  # ty: ignore[deprecated]


def provider_checks() -> None:
    """Pass `provider_checks=`, or neither, with nothing reported."""
    HealthChecks()
    HealthChecks(provider_checks="require")
    HealthChecks.from_config(HealthChecksConfig(), provider_checks="off")
