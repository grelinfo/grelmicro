"""Health Check Registry."""

from typing import Annotated

import anyio
from typing_extensions import Doc

from grelmicro.health._models import (
    ComponentHealth,
    HealthReport,
    HealthStatus,
    OverallStatus,
)
from grelmicro.health._protocol import HealthChecker
from grelmicro.health.errors import HealthCheckTimeoutError


class HealthRegistry:
    """Registry that manages health checkers and runs them concurrently.

    All registered checkers are executed in parallel via an anyio task
    group.  Each checker has an individual timeout; checkers that time
    out or raise are reported as ``UNHEALTHY`` with an error detail.
    """

    def __init__(
        self,
        *,
        timeout: Annotated[
            float,
            Doc(
                "Per-checker timeout in seconds. Checkers that exceed "
                "this duration are reported as UNHEALTHY."
            ),
        ] = 5.0,
    ) -> None:
        """Initialize the health registry."""
        self._checkers: dict[str, HealthChecker] = {}
        self._timeout = timeout

    def add(
        self,
        checker: Annotated[
            HealthChecker,
            Doc("A health checker to register."),
        ],
    ) -> None:
        """Register a health checker.

        Raises:
            ValueError: If a checker with the same name is already
                registered.
        """
        if checker.name in self._checkers:
            msg = f"Health checker '{checker.name}' is already registered"
            raise ValueError(msg)
        self._checkers[checker.name] = checker

    async def check(self) -> HealthReport:
        """Run all registered checkers concurrently.

        Each checker runs with an individual timeout. Checkers that
        raise or time out produce an UNHEALTHY component entry.

        Returns:
            A HealthReport containing the aggregated status.
        """
        results: list[ComponentHealth] = []

        async def _run_checker(checker: HealthChecker) -> None:
            try:
                with anyio.move_on_after(self._timeout) as cancel_scope:
                    status = await checker.check()
                if cancel_scope.cancelled_caught:
                    error = HealthCheckTimeoutError(
                        name=checker.name, timeout=self._timeout
                    )
                    results.append(
                        ComponentHealth(
                            name=checker.name,
                            status=HealthStatus.UNHEALTHY,
                            detail=str(error),
                        )
                    )
                else:
                    results.append(
                        ComponentHealth(
                            name=checker.name,
                            status=status,
                            detail=None,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    ComponentHealth(
                        name=checker.name,
                        status=HealthStatus.UNHEALTHY,
                        detail=str(exc),
                    )
                )

        async with anyio.create_task_group() as tg:
            for checker in self._checkers.values():
                tg.start_soon(_run_checker, checker)

        all_healthy = all(c["status"] == HealthStatus.HEALTHY for c in results)

        return HealthReport(
            status=(
                OverallStatus.HEALTHY if all_healthy else OverallStatus.DEGRADED
            ),
            components=sorted(results, key=lambda c: c["name"]),
        )
