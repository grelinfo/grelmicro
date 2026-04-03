"""Health Checker Protocol."""

from typing import Protocol

from grelmicro.health._models import HealthStatus


class HealthChecker(Protocol):
    """Protocol for health check implementations.

    Any class with a ``name`` property and an async ``check`` method
    satisfies this protocol (structural subtyping).

    Checkers should raise an exception to signal unhealthy status.
    The registry catches exceptions and maps them to
    ``HealthStatus.UNHEALTHY`` with the error message as detail.
    """

    @property
    def name(self) -> str:
        """Unique name identifying this health check."""
        ...

    async def check(self) -> HealthStatus:
        """Run the health check.

        Returns:
            HealthStatus.HEALTHY if the component is healthy.

        Raises:
            Exception: Any exception signals the component is unhealthy.
                The exception message is captured as the detail field.
        """
        ...
