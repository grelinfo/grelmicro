"""Rate Limiter."""

import logging
from typing import Annotated, assert_never

from typing_extensions import Doc

from grelmicro.resilience._backends import get_rate_limiter_backend
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import (
    GCRAConfig,
    RateLimiterConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.errors import RateLimitExceededError

logger = logging.getLogger(__name__)


class RateLimiter:
    """Rate limiter with a pluggable algorithm.

    Construct it with the instance name and a discriminated
    algorithm configuration:
    [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
    for burst-friendly semantics, or
    [`GCRAConfig`][grelmicro.resilience.algorithms.GCRAConfig] for
    precise sliding-window semantics.

    The algorithm is bound to the backend once at construction via
    [`RateLimiterBackend.bind`][grelmicro.resilience.RateLimiterBackend.bind].
    Each call to `acquire`, `peek`, or `reset` then runs the bound
    strategy directly. There is no extra algorithm lookup on each
    call.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, TokenBucketConfig
    from grelmicro.resilience.memory import MemoryRateLimiterBackend

    MemoryRateLimiterBackend()
    api = RateLimiter("api", TokenBucketConfig(capacity=10, refill_rate=1))


    async def handle(user_id: str) -> None:
        result = await api.acquire(key=user_id)
        if not result.allowed:
            raise RuntimeError("rate limited")
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                """
                The name of the rate limiter instance.

                Acts as the instance identity. Used as the key
                prefix on the backend and exposed via the `name`
                property.
                """
            ),
        ],
        config: Annotated[
            RateLimiterConfig,
            Doc(
                """
                The algorithm configuration.

                Pass a
                [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
                or a
                [`GCRAConfig`][grelmicro.resilience.algorithms.GCRAConfig].
                Both carry algorithm parameters plus the shared
                `fail_open` setting. The classes share a
                discriminated `type` field so serialization
                round-trips and pydantic-settings composition both
                work.
                """
            ),
        ],
        *,
        backend: Annotated[
            RateLimiterBackend | None,
            Doc(
                """
                An explicit backend instance. When `None` (the
                default), the registered backend is used.

                Set this to skip the global registry, for example
                in tests or when running several backends at the
                same time.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the rate limiter."""
        self._name = name
        self._config = config
        self._backend = backend or get_rate_limiter_backend()
        self._strategy: RateLimiterStrategy = self._backend.bind(config)
        self._fail_open = config.fail_open
        self._fallback = _build_fallback(config)

    @property
    def name(self) -> str:
        """Return the rate limiter identity."""
        return self._name

    @property
    def config(self) -> RateLimiterConfig:
        """Return the algorithm configuration."""
        return self._config

    def _log_fail_open(
        self,
        key: str,
        exc: Exception,
    ) -> None:
        """Log a fail-open warning for a backend error."""
        logger.warning(
            "Rate limiter '%s' backend error, failing open for key '%s'",
            self._name,
            key,
            exc_info=exc,
        )

    def _full_key(self, key: str) -> str:
        return f"{self._name}:{key}"

    async def acquire(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
            ),
        ],
        cost: Annotated[
            int,
            Doc("Number of tokens to consume."),
        ] = 1,
    ) -> RateLimitResult:
        """Check rate limit and consume tokens if allowed.

        Returns:
            RateLimitResult with allowed, limit, remaining,
            retry_after, and reset_after fields.

        Raises:
            ValueError: If `cost` is not between 1 and the
                algorithm's limit/capacity.
        """
        _validate_cost(cost, self._fallback.limit)
        try:
            return await self._strategy.acquire(
                key=self._full_key(key), cost=cost
            )
        except Exception as exc:
            if self._fail_open:
                self._log_fail_open(key, exc)
                return self._fallback
            raise

    async def acquire_or_raise(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
            ),
        ],
        cost: Annotated[
            int,
            Doc("Number of tokens to consume."),
        ] = 1,
    ) -> RateLimitResult:
        """Check rate limit, raise if exceeded.

        Returns:
            RateLimitResult if allowed.

        Raises:
            RateLimitExceededError: If the rate limit is exceeded.
        """
        result = await self.acquire(key=key, cost=cost)
        if not result.allowed:
            raise RateLimitExceededError(
                key=key,
                retry_after=result.retry_after,
            )
        return result

    async def peek(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
            ),
        ],
    ) -> RateLimitResult:
        """Check rate limit state without consuming tokens.

        Returns:
            RateLimitResult reflecting the current state.
            `allowed` indicates whether the next acquire would
            succeed.
        """
        try:
            return await self._strategy.peek(key=self._full_key(key))
        except Exception as exc:
            if self._fail_open:
                self._log_fail_open(key, exc)
                return self._fallback
            raise

    async def reset(
        self,
        *,
        key: Annotated[
            str,
            Doc(
                "Identifier for rate limiting"
                " (e.g. IP address, user ID, session)."
            ),
        ],
    ) -> None:
        """Delete rate limit state for a key, restoring full quota.

        Idempotent: resetting a nonexistent key is a no-op.
        """
        try:
            await self._strategy.reset(key=self._full_key(key))
        except Exception as exc:
            if self._fail_open:
                self._log_fail_open(key, exc)
                return
            raise


def _build_fallback(config: RateLimiterConfig) -> RateLimitResult:
    """Build the fail-open fallback result for the given algorithm config.

    Called once when
    [`RateLimiter`][grelmicro.resilience.RateLimiter] is created.
    The result is cached on the instance and reused on every
    fail-open path.
    """
    match config:
        case TokenBucketConfig():
            limit_value = config.capacity
        case GCRAConfig():
            limit_value = config.limit
        case _ as unknown:  # pragma: no cover
            assert_never(unknown)
    return RateLimitResult(
        allowed=True,
        limit=limit_value,
        remaining=limit_value,
        retry_after=0.0,
        reset_after=0.0,
    )


def _validate_cost(cost: int, limit: int) -> None:
    """Validate that cost is within `[1, limit]`."""
    if cost < 1 or cost > limit:
        msg = f"cost must be between 1 and {limit}, got {cost}"
        raise ValueError(msg)
