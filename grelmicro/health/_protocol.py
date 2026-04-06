"""Health Checker Protocol."""

from typing import Any, Protocol


class HealthChecker(Protocol):
    """Protocol for health check implementations.

    Any class with a ``name`` property and an async ``check`` method
    satisfies this protocol (structural subtyping).

    - Return ``None`` to signal healthy with no details.
    - Return a ``dict`` to signal healthy with details (e.g. metrics).
    - Raise a ``HealthError`` to signal unhealthy with a specific
      message exposed in the ``error`` field.
    - Raise any other exception to signal unhealthy with a generic
      ``"Health check failed"`` message (details are logged server-side).
    """

    @property
    def name(self) -> str:
        """Unique name identifying this health check."""
        ...

    async def check(self) -> dict[str, Any] | None:
        """Run the health check.

        Returns:
            ``None`` if the component is healthy with no details,
            or a ``dict`` with optional details (e.g. latency, version).

        Raises:
            HealthError: Signals unhealthy with the exception message
                exposed in the ``error`` field.
            Exception: Any other exception signals unhealthy with a
                generic ``"Health check failed"`` message.
        """
        ...
