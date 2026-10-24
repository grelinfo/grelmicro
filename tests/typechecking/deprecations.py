"""Type assertions for the names and arguments removed in 0.44.0.

Each marked line uses something deprecated, so ty and mypy report it. The
ignore comment is the assertion: both checkers fail on an ignore comment
that matches nothing, so a lost `@deprecated` fails the check.
"""

import grelmicro.log
import grelmicro.log.config
import grelmicro.metrics
import grelmicro.trace
from grelmicro.health import HealthChecks, HealthChecksConfig
from grelmicro.log import LogLevelType  # type: ignore[deprecated]  # ty: ignore[deprecated]
from grelmicro.metrics.config import MetricsExporterType  # type: ignore[deprecated]  # ty: ignore[deprecated]
from grelmicro.trace import TraceSamplerType  # type: ignore[deprecated]  # ty: ignore[deprecated]


def imported() -> None:
    """Use a name imported from its package or its config module."""
    print(LogLevelType.INFO)  # ty: ignore[deprecated]  # noqa: T201
    print(MetricsExporterType.AUTO)  # ty: ignore[deprecated]  # noqa: T201
    print(TraceSamplerType.ALWAYS_ON)  # ty: ignore[deprecated]  # noqa: T201


def module_attributes() -> None:
    """Read each old enum as an attribute of the module that serves it."""
    print(grelmicro.metrics.MetricsExporterType)  # type: ignore[deprecated]  # ty: ignore[deprecated]  # noqa: T201
    print(grelmicro.trace.TraceExporterType)  # type: ignore[deprecated]  # ty: ignore[deprecated]  # noqa: T201
    print(grelmicro.trace.TraceProcessorType)  # type: ignore[deprecated]  # ty: ignore[deprecated]  # noqa: T201
    print(grelmicro.log.LogBackendType)  # type: ignore[deprecated]  # ty: ignore[deprecated]  # noqa: T201
    print(grelmicro.log.LogFormatType)  # type: ignore[deprecated]  # ty: ignore[deprecated]  # noqa: T201
    print(grelmicro.log.config.LogSerializerType)  # type: ignore[deprecated]  # ty: ignore[deprecated]  # noqa: T201


def auto_health() -> None:
    """Pass `auto_health=`, renamed to `provider_checks=`."""
    HealthChecks(auto_health=True)  # type: ignore[deprecated]  # ty: ignore[deprecated]
    HealthChecks.from_config(HealthChecksConfig(), auto_health=False)  # type: ignore[deprecated]  # ty: ignore[deprecated]


def provider_checks() -> None:
    """Pass `provider_checks=`, or neither, with nothing reported."""
    HealthChecks()
    HealthChecks(provider_checks="require")
    HealthChecks.from_config(HealthChecksConfig(), provider_checks="off")
