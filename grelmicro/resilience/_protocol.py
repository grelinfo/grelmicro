"""Rate Limiter, Circuit Breaker, and Retry Protocols."""

from types import TracebackType
from typing import TYPE_CHECKING, NamedTuple, Protocol, Self, runtime_checkable

from grelmicro.resilience.algorithms import RateLimiterConfig

if TYPE_CHECKING:
    from grelmicro.resilience.circuitbreaker import CircuitBreaker


class RetryStrategy(Protocol):
    """A retry strategy for a specific backoff algorithm.

    Built once per retry loop from a backoff config. The strategy
    holds any state the algorithm needs (for example the previous
    delay for decorrelated jitter) and computes one delay per
    upcoming attempt.
    """

    def delay(self, attempt: int) -> float:
        """Return the delay in seconds before retry ``attempt``.

        ``attempt`` is the upcoming retry number. ``attempt=1`` is
        the delay before the first retry (after the initial call
        failed). The strategy may apply jitter and clamp to its
        configured maximum.
        """
        ...


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
    """Total quota (`capacity` for TokenBucketConfig, `limit` for GCRAConfig)."""

    remaining: int
    """Remaining tokens or requests."""

    retry_after: float
    """Seconds until the next request is allowed (0.0 if allowed)."""

    reset_after: float
    """Seconds until the full quota resets."""


class RateLimiterStrategy(Protocol):
    """A rate-limiter strategy for a specific algorithm and backend.

    Returned by
    [`RateLimiterBackend.bind`][grelmicro.resilience.RateLimiterBackend.bind].
    The algorithm settings are already stored in the strategy,
    so the methods only need `key` and `cost`. No extra
    algorithm lookup happens at call time.
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


@runtime_checkable
class RateLimiterBackend(Protocol):
    """Protocol for rate-limiter storage backends.

    A backend holds the storage for every rate limiter that uses
    it. It turns an algorithm into a strategy through
    [`bind`][grelmicro.resilience.RateLimiterBackend.bind]. The
    returned
    [`RateLimiterStrategy`][grelmicro.resilience.RateLimiterStrategy]
    is what a [`RateLimiter`][grelmicro.resilience.RateLimiter]
    calls on each `acquire`, `peek`, or `reset`. No extra
    algorithm lookup happens at call time.
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
        config: RateLimiterConfig,
    ) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm config.

        Called exactly once per
        [`RateLimiter`][grelmicro.resilience.RateLimiter] when
        it is created. The returned strategy shares storage with
        the backend. Later requests call the strategy methods
        directly, with no extra algorithm lookup.

        Args:
            config: The algorithm configuration
                (`TokenBucketConfig` or `GCRAConfig`).

        Returns:
            A strategy bound to `config` and this backend's storage.
        """
        ...


@runtime_checkable
class CircuitBreakerBackend(Protocol):
    """Protocol for circuit-breaker storage backends.

    A backend owns the lifespan boundary for every circuit breaker
    bound to it. The in-memory implementation keeps state in process.
    A future Redis-backed implementation will share state across
    replicas.

    Implementations capture the running event loop on ``__aenter__``
    in a ``_loop`` attribute so the sync ``from_thread`` adapter can
    dispatch coroutines back into it.
    """

    async def __aenter__(self) -> Self:
        """Open the backend."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backend, releasing per-breaker state."""
        ...

    def register(self, breaker: "CircuitBreaker") -> None:
        """Bind a breaker to this backend so it is reset on close."""
        ...
