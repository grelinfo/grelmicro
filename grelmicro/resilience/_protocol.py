"""Rate Limiter Backend Protocol."""

from types import TracebackType
from typing import NamedTuple, Protocol, Self


class RateLimitResult(NamedTuple):
    """Result of a rate limit check.

    Fields map to HTTP rate limit headers:
    - ``allowed`` -> 200 vs 429 status
    - ``limit`` -> ``X-RateLimit-Limit`` / ``RateLimit-Policy: ;q=``
    - ``remaining`` -> ``X-RateLimit-Remaining`` / ``RateLimit: ;r=``
    - ``retry_after`` -> ``Retry-After`` header
    - ``reset_after`` -> ``X-RateLimit-Reset`` / ``RateLimit: ;t=``
    """

    allowed: bool
    """Whether the request is permitted."""

    limit: int
    """Total quota for the window."""

    remaining: int
    """Remaining requests in the current window."""

    retry_after: float
    """Seconds until the next request is allowed (0.0 if allowed)."""

    reset_after: float
    """Seconds until the full quota resets."""


class RateLimiterBackend(Protocol):
    """Protocol for rate limiter storage backends.

    Implementations must be async context managers and provide
    a single ``acquire`` method for atomic rate limit checks.
    """

    async def __aenter__(self) -> Self:
        """Open the backend connection."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backend connection."""
        ...

    async def acquire(
        self,
        *,
        key: str,
        limit: int,
        window: float,
        cost: int,
    ) -> RateLimitResult:
        """Try to acquire rate limit tokens.

        Args:
            key: The rate limit key (e.g. IP, user ID, session).
            limit: Maximum number of requests allowed in the window.
            window: Window duration in seconds.
            cost: Number of tokens to consume.

        Returns:
            RateLimitResult with allowed, limit, remaining,
            retry_after, and reset_after fields.
        """
        ...
