"""Health Check Registry."""

from dataclasses import dataclass
from logging import getLogger
from typing import Annotated, Any

import anyio
from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc

from grelmicro.health._models import (
    ComponentHealth,
    HealthReport,
    HealthStatus,
    OverallStatus,
)
from grelmicro.health._protocol import HealthChecker
from grelmicro.health.errors import HealthCheckTimeoutError, HealthError

logger = getLogger("grelmicro.health")


class HealthRegistryConfig(BaseModel, frozen=True, extra="forbid"):
    """Health Registry Config."""

    timeout: Annotated[
        PositiveFloat,
        Doc(
            "Per-checker timeout in seconds. Checkers that exceed "
            "this duration are reported as UNHEALTHY."
        ),
    ] = 5.0


@dataclass(frozen=True, slots=True)
class _RegisteredChecker:
    """A checker with its registration metadata."""

    checker: HealthChecker
    critical: bool


class HealthRegistry:
    """Registry that manages health checkers and runs them concurrently.

    All registered checkers are executed in parallel via an anyio task
    group.  Each checker has an individual timeout; checkers that time
    out or raise are reported as ``UNHEALTHY`` with an error message.
    """

    def __init__(
        self,
        *,
        timeout: Annotated[
            PositiveFloat,
            Doc(
                "Per-checker timeout in seconds. Checkers that exceed "
                "this duration are reported as UNHEALTHY."
            ),
        ] = 5.0,
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register this instance as the global "
                "health registry singleton. Set to False for testing."
            ),
        ] = True,
    ) -> None:
        """Initialize the health registry."""
        self._config = HealthRegistryConfig(timeout=timeout)
        self._checkers: dict[str, _RegisteredChecker] = {}
        if auto_register:
            from grelmicro.health._state import set_health_registry  # noqa: I001, PLC0415

            set_health_registry(self)

    def add(
        self,
        checker: Annotated[
            HealthChecker,
            Doc("A health checker to register."),
        ],
        *,
        critical: Annotated[
            bool,
            Doc(
                "Whether this checker affects the overall health status. "
                "Non-critical checkers are still executed and reported, "
                "but their failures do not cause the overall status to "
                "become DEGRADED."
            ),
        ] = True,
    ) -> None:
        """Register a health checker.

        Raises:
            ValueError: If a checker with the same name is already
                registered.
        """
        if checker.name in self._checkers:
            msg = f"Health checker '{checker.name}' is already registered"
            raise ValueError(msg)
        self._checkers[checker.name] = _RegisteredChecker(
            checker=checker, critical=critical
        )
        self._checkers = dict(sorted(self._checkers.items()))

    async def check(self) -> HealthReport:
        """Run all registered checkers concurrently.

        Each checker runs with an individual timeout. Checkers that
        raise or time out produce an UNHEALTHY component entry.

        Only critical checkers affect the overall status.

        Returns:
            A HealthReport containing the aggregated status.
        """
        timeout = self._config.timeout
        entries = list(self._checkers.values())
        results: dict[int, ComponentHealth] = {}

        async def _run_checker(index: int, entry: _RegisteredChecker) -> None:
            checker = entry.checker
            try:
                with anyio.move_on_after(timeout) as cancel_scope:
                    result: dict[str, Any] | None = await checker.check()
                if cancel_scope.cancelled_caught:
                    error = HealthCheckTimeoutError(
                        name=checker.name, timeout=timeout
                    )
                    results[index] = ComponentHealth(
                        name=checker.name,
                        status=HealthStatus.UNHEALTHY,
                        critical=entry.critical,
                        error=str(error),
                        details=None,
                    )
                else:
                    results[index] = ComponentHealth(
                        name=checker.name,
                        status=HealthStatus.HEALTHY,
                        critical=entry.critical,
                        error=None,
                        details=result,
                    )
            except HealthError as exc:
                results[index] = ComponentHealth(
                    name=checker.name,
                    status=HealthStatus.UNHEALTHY,
                    critical=entry.critical,
                    error=str(exc),
                    details=None,
                )
            except Exception:
                logger.exception(
                    "Health check '%s' raised unexpectedly",
                    checker.name,
                )
                results[index] = ComponentHealth(
                    name=checker.name,
                    status=HealthStatus.UNHEALTHY,
                    critical=entry.critical,
                    error="Health check failed",
                    details=None,
                )

        async with anyio.create_task_group() as tg:
            for i, entry in enumerate(entries):
                tg.start_soon(_run_checker, i, entry)

        components = [results[i] for i in range(len(entries))]
        all_critical_healthy = all(
            c["status"] == HealthStatus.HEALTHY
            for c in components
            if c["critical"]
        )

        return HealthReport(
            status=(
                OverallStatus.HEALTHY
                if all_critical_healthy
                else OverallStatus.DEGRADED
            ),
            components=components,
        )
