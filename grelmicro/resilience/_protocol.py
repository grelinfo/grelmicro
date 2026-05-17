"""Rate Limiter, Circuit Breaker, and Retry Protocols."""

from types import TracebackType
from typing import (
    TYPE_CHECKING,
    ClassVar,
    NamedTuple,
    Protocol,
    Self,
    runtime_checkable,
)

from grelmicro.resilience.algorithms import RateLimiterConfig

if TYPE_CHECKING:
    from grelmicro.resilience.circuitbreaker import (
        CircuitBreaker,
        CircuitBreakerConfig,
        CircuitBreakerState,
    )


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
    """Total quota (`capacity` for TokenBucketConfig, `limit` for SlidingWindowConfig)."""

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
                (`TokenBucketConfig` or `SlidingWindowConfig`).

        Returns:
            A strategy bound to `config` and this backend's storage.
        """
        ...


class CircuitBreakerSnapshot(NamedTuple):
    """Snapshot of circuit breaker state returned by a strategy.

    Returned by every
    [`CircuitBreakerStrategy`][grelmicro.resilience.CircuitBreakerStrategy]
    method that mutates or reads shared state. The breaker uses it to
    refresh its local cache so reads of `cb.state` and `cb.metrics()`
    reflect the latest shared truth on this replica.

    Algorithm-specific counters (`consecutive_error_count`,
    `consecutive_success_count`) are populated by the consecutive-count
    algorithm. Future algorithms may populate additional fields.
    """

    state: "CircuitBreakerState"
    """Authoritative state for the breaker."""

    opened_at: float
    """Server-side epoch seconds when the breaker entered OPEN. 0.0 when not OPEN."""

    consecutive_error_count: int = 0
    """Consecutive errors recorded by the strategy. Consecutive-count algorithm only."""

    consecutive_success_count: int = 0
    """Consecutive successes recorded by the strategy. Consecutive-count algorithm only."""


class CircuitBreakerStrategy(Protocol):
    """A circuit-breaker strategy for a specific algorithm and backend.

    Returned by
    [`CircuitBreakerBackend.bind`][grelmicro.resilience.CircuitBreakerBackend.bind].
    The breaker name and algorithm settings are already stored in the
    strategy, so the methods take no extra arguments beyond the call's
    own data (outcome, manual transition target).
    """

    async def try_acquire(self) -> bool:
        """Attempt to admit a call.

        Returns True when the call is admitted. Returns False when the
        breaker is OPEN or FORCED_OPEN, or when HALF_OPEN has no
        remaining capacity.
        """
        ...

    async def record_outcome(
        self,
        *,
        success: bool,
        duration: float = 0.0,
    ) -> "CircuitBreakerSnapshot":
        """Record a call outcome and return the resulting snapshot.

        Args:
            success: Whether the call completed without an error that
                counts against the breaker.
            duration: Wall-clock seconds the call took. Consumed by
                algorithms that classify slow calls; ignored by the
                consecutive-count algorithm.
        """
        ...

    async def transition(
        self,
        *,
        desired: "CircuitBreakerState",
        cool_down: float | None = None,
    ) -> None:
        """Force the breaker into ``desired``.

        Args:
            desired: Target state.
            cool_down: Seconds the breaker should stay OPEN before
                transitioning to HALF_OPEN. When ``None`` the strategy
                uses its configured ``reset_timeout``. Ignored for
                non-OPEN targets.
        """
        ...

    async def get_snapshot(self) -> "CircuitBreakerSnapshot":
        """Return the current snapshot without mutating state."""
        ...


@runtime_checkable
class CircuitBreakerBackend(Protocol):
    """Protocol for circuit-breaker storage backends.

    A backend owns the lifespan boundary for every circuit breaker
    bound to it. It turns an algorithm config into a strategy through
    [`bind`][grelmicro.resilience.CircuitBreakerBackend.bind]. The
    returned
    [`CircuitBreakerStrategy`][grelmicro.resilience.CircuitBreakerStrategy]
    is what a
    [`CircuitBreaker`][grelmicro.resilience.CircuitBreaker] calls on
    each `try_acquire`, `record_outcome`, `transition`, or
    `get_snapshot`.

    Implementations capture the running event loop on ``__aenter__``
    in a ``_loop`` attribute so the sync ``from_thread`` adapter can
    dispatch coroutines back into it.
    """

    is_shared: ClassVar[bool]
    """Whether the backend stores state outside the local process."""

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

    def bind(
        self,
        *,
        name: str,
        config: "CircuitBreakerConfig",
    ) -> CircuitBreakerStrategy:
        """Build a strategy for the named breaker and algorithm config.

        Called once per
        [`CircuitBreaker`][grelmicro.resilience.CircuitBreaker] the
        first time it enters a shared backend, and again whenever the
        breaker's config changes through live reconfiguration.
        """
        ...
