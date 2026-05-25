"""Rate Limiter, Circuit Breaker, and Retry Protocols.

This module defines `typing.Protocol` types. Methods end with `...`
because the protocols describe structural contracts, not
implementations. Concrete strategies and backends (e.g.
`_MemoryTokenBucket`, `RedisRateLimiterAdapter`,
`_ExponentialStrategy`) provide the bodies.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    ClassVar,
    NamedTuple,
    Protocol,
    Self,
    runtime_checkable,
)

from typing_extensions import Doc

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.resilience.circuitbreaker import (
        CircuitBreakerConfig,
        CircuitBreakerState,
    )
    from grelmicro.resilience.ratelimiter import RateLimiterConfig


class RetryStrategy(Protocol):
    """A retry strategy for a specific backoff algorithm.

    Built once per retry loop from a backoff config. The strategy
    holds any state the algorithm needs (for example the previous
    delay for decorrelated jitter) and computes one delay per
    upcoming attempt.
    """

    def delay(
        self,
        attempt: Annotated[
            int,
            Doc(
                "Upcoming retry number. `attempt=1` is the delay before"
                " the first retry, after the initial call failed."
            ),
        ],
    ) -> float:
        """Return the delay in seconds before retry `attempt`.

        The strategy may apply jitter and clamp to its configured
        maximum.
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
    """Remaining tokens or requests.

    For algorithms with continuous state (`SlidingWindowConfig`,
    GCRA-based strategies) this is an estimate rounded to the nearest
    whole request. Enforcement still uses the exact internal state, so
    the next `acquire` may be denied even when `remaining > 0`.
    """

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
        key: Annotated[
            str,
            Doc("Rate-limit key (e.g. IP, user ID, session)."),
        ],
        cost: Annotated[
            int,
            Doc("Number of tokens to consume on this request."),
        ],
    ) -> RateLimitResult:
        """Try to acquire rate-limit tokens for `key`.

        Returns a `RateLimitResult` with `allowed`, `limit`,
        `remaining`, `retry_after`, and `reset_after`.
        """
        ...

    async def peek(
        self,
        *,
        key: Annotated[
            str,
            Doc("Rate-limit key to inspect."),
        ],
    ) -> RateLimitResult:
        """Return the current state for `key` without consuming tokens."""
        ...

    async def reset(
        self,
        *,
        key: Annotated[
            str,
            Doc("Rate-limit key to reset to full quota."),
        ],
    ) -> None:
        """Delete rate-limit state for `key`, restoring full quota."""
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
        config: Annotated[
            RateLimiterConfig,
            Doc(
                "Algorithm configuration."
                " `TokenBucketConfig` or `SlidingWindowConfig`."
            ),
        ],
    ) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm config.

        Called exactly once per
        [`RateLimiter`][grelmicro.resilience.RateLimiter] when it is
        created. The returned strategy shares storage with the
        backend. Later requests call the strategy methods directly,
        with no extra algorithm lookup.
        """
        ...


class CircuitBreakerSnapshot(NamedTuple):
    """Snapshot of circuit breaker state returned by a strategy.

    Returned by every
    [`CircuitBreakerStrategy`][grelmicro.resilience.CircuitBreakerStrategy]
    method that mutates or reads state. The breaker uses it to refresh
    its local cache so reads of `cb.state` and `cb.metrics()` reflect
    the latest truth from the strategy.

    Algorithm-specific counters (`consecutive_error_count`,
    `consecutive_success_count`) are populated by the consecutive-count
    algorithm. Future algorithms may populate additional fields.
    """

    state: CircuitBreakerState
    """Authoritative state for the breaker."""

    opened_at: float
    """Strategy-clock seconds when the breaker entered OPEN. 0.0 when not OPEN.

    Each strategy picks its own clock. Treat this as a relative value:
    only compare with timestamps emitted by the same strategy.
    """

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
        success: Annotated[
            bool,
            Doc(
                "Whether the call completed without an error that counts"
                " against the breaker."
            ),
        ],
        duration: Annotated[
            float,
            Doc(
                "Wall-clock seconds the call took. Consumed by algorithms"
                " that classify slow calls. Ignored by the"
                " consecutive-count algorithm."
            ),
        ] = 0.0,
    ) -> CircuitBreakerSnapshot:
        """Record a call outcome and return the resulting snapshot."""
        ...

    async def transition(
        self,
        *,
        desired: Annotated[
            CircuitBreakerState,
            Doc("Target state to force the breaker into."),
        ],
        cool_down: Annotated[
            float | None,
            Doc(
                "Seconds the breaker stays OPEN before moving to"
                " HALF_OPEN. `None` uses the configured `reset_timeout`."
                " Ignored when `desired` is not OPEN."
            ),
        ] = None,
    ) -> None:
        """Force the breaker into `desired`."""
        ...

    async def get_snapshot(self) -> CircuitBreakerSnapshot:
        """Return the current snapshot without mutating state."""
        ...


@runtime_checkable
class CircuitBreakerBackend(Protocol):
    """Protocol for circuit-breaker storage backends.

    A backend owns the lifespan boundary and the storage for every
    circuit breaker bound to it. It turns a name and a config into a
    strategy through
    [`bind`][grelmicro.resilience.CircuitBreakerBackend.bind]. The
    returned
    [`CircuitBreakerStrategy`][grelmicro.resilience.CircuitBreakerStrategy]
    is what a
    [`CircuitBreaker`][grelmicro.resilience.CircuitBreaker] calls on
    each `try_acquire`, `record_outcome`, `transition`, or
    `get_snapshot`. No extra algorithm lookup happens at call time.

    Implementations capture the running event loop on ``__aenter__``
    in a ``_loop`` attribute so the sync ``from_thread`` adapter can
    dispatch coroutines back into it.
    """

    is_shared: ClassVar[bool]
    """Whether the backend stores state outside the local process.

    `True` for distributed backends (e.g. Redis), `False` for
    process-local backends (e.g. memory). User code can read this
    to decide whether `last_error` is per-replica or fleet-wide.
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

    def bind(
        self,
        *,
        name: Annotated[
            str,
            Doc("Breaker name, used as the storage key in shared backends."),
        ],
        config: Annotated[
            CircuitBreakerConfig,
            Doc("Algorithm configuration for the breaker."),
        ],
    ) -> CircuitBreakerStrategy:
        """Build a strategy for the named breaker and algorithm config.

        Called once per
        [`CircuitBreaker`][grelmicro.resilience.CircuitBreaker] when
        it is created, and again whenever the breaker's config changes
        through live reconfiguration.
        """
        ...
