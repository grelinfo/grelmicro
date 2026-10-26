"""Type assertions for the arguments removed in 0.44.0.

Each marked line uses something deprecated, so ty and mypy report it. The
ignore comment is the assertion: both checkers fail on an ignore comment
that matches nothing, so a lost `@deprecated` fails the check.
"""

from grelmicro.health import HealthChecks, HealthChecksConfig


def provider_checks() -> None:
    """Pass `provider_checks=`, or neither, with nothing reported."""
    HealthChecks()
    HealthChecks(provider_checks="require")
    HealthChecks.from_config(HealthChecksConfig(), provider_checks="off")
