"""Rate Limiter Backend and Strategy Protocols."""

from types import TracebackType
from typing import NamedTuple, Protocol, Self

from grelmicro.resilience.algorithms import Algorithm


class RateLimitResult(NamedTuple):
    """Result of a rate limit check.

    Fields map to HTTP rate limit headers:
    - `allowed` -> 200 vs 429 status
    - `limit` -> `X-RateLimit-Limit` / `RateLimit-Policy: ;q=`
    - `remaining` -> `X-RateLimit-Remaining` / `RateLimit: ;r=`
    - `retry_after` -> `Retry-After` header
    - `reset_after` -> `X-RateLimit-Reset` / `RateLimit: ;t=`
    """

    allowed: bool
    """Whether the request is permitted."""

    limit: int
    """Total quota (`capacity` for TokenBucket, `limit` for GCRA)."""

    remaining: int
    """Remaining tokens or requests."""

    retry_after: float
    """Seconds until the next request is allowed (0.0 if allowed)."""

    reset_after: float
    """Seconds until the full quota resets."""


class RateLimiterStrategy(Protocol):
    """Algorithm- and backend-specific rate-limiter strategy.

    Produced by
    [`RateLimiterBackend.bind`][grelmicro.resilience.RateLimiterBackend.bind].
    The algorithm config is already baked in, so methods take
    only `key` and `cost`. Runtime dispatch is zero.
    """

    async def acquire(
        self,
        *,
        key: str,
        cost: int,
    ) -> RateLimitResult:
        """Try to acquire rate limit tokens.

        Args:
            key: The rate limit key (e.g. IP, user ID, session).
            cost: Number of tokens to consume.

        Returns:
            RateLimitResult with allowed, limit, remaining,
            retry_after, and reset_after fields.
        """
        ...

    async def peek(
        self,
        *,
        key: str,
    ) -> RateLimitResult:
        """Check rate limit state without consuming tokens.

        Args:
            key: The rate limit key.

        Returns:
            RateLimitResult reflecting the current state.
        """
        ...

    async def reset(
        self,
        *,
        key: str,
    ) -> None:
        """Delete rate limit state for a key, restoring full quota.

        Args:
            key: The rate limit key to reset.
        """
        ...


class RateLimiterBackend(Protocol):
    """Protocol for rate-limiter storage backends.

    A backend owns the storage for every rate limiter that shares
    it. It compiles an algorithm into a bound strategy via
    [`bind`][grelmicro.resilience.RateLimiterBackend.bind]; the
    returned
    [`RateLimiterStrategy`][grelmicro.resilience.RateLimiterStrategy]
    is what a [`RateLimiter`][grelmicro.resilience.RateLimiter]
    calls on every `acquire` / `peek` / `reset`, with no algorithm
    dispatch at runtime.
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

    def bind(
        self,
        algorithm: Algorithm,
    ) -> RateLimiterStrategy:
        """Compile an algorithm into a bound strategy.

        Called exactly once per
        [`RateLimiter`][grelmicro.resilience.RateLimiter] at
        construction. The returned strategy keeps a reference to
        the backend's live storage; subsequent requests invoke
        strategy methods directly without any algorithm dispatch.

        Args:
            algorithm: The algorithm configuration
                (`TokenBucket` or `GCRA`).

        Returns:
            A strategy bound to `algorithm` and this backend's
            storage.
        """
        ...
