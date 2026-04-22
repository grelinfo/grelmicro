"""Rate Limiter."""

import logging
import warnings
from typing import Annotated, assert_never

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc, deprecated

from grelmicro.resilience._backends import get_rate_limiter_backend
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import GCRA, Algorithm, TokenBucket
from grelmicro.resilience.errors import RateLimitExceededError

logger = logging.getLogger(__name__)


class RateLimiterConfig(BaseModel, frozen=True, extra="forbid"):
    """Rate Limiter Config."""

    name: Annotated[
        str,
        Doc("The name of the rate limiter instance."),
    ]
    algorithm: Annotated[
        Algorithm,
        Doc(
            """
            The rate-limit algorithm: an instance of
            [`TokenBucket`][grelmicro.resilience.algorithms.TokenBucket]
            or [`GCRA`][grelmicro.resilience.algorithms.GCRA].
            """
        ),
    ]


class RateLimiter:
    """Rate limiter with a pluggable algorithm.

    Supports multiple algorithms via the required `algorithm`
    parameter: pass a
    [`TokenBucket`][grelmicro.resilience.algorithms.TokenBucket]
    for burst-friendly semantics, or a
    [`GCRA`][grelmicro.resilience.algorithms.GCRA] for precise
    sliding-window semantics.

    The algorithm is attached to the backend once when the
    rate limiter is created. It uses
    [`RateLimiterBackend.bind`][grelmicro.resilience.RateLimiterBackend.bind].
    Each call to `acquire`, `peek`, or `reset` then runs the
    attached algorithm directly. There is no extra algorithm
    lookup on each call.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, TokenBucket
    from grelmicro.resilience.memory import MemoryRateLimiterBackend

    MemoryRateLimiterBackend()
    api = RateLimiter(
        "api", algorithm=TokenBucket(capacity=10, refill_rate=1)
    )


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
            Doc("Name of the rate limiter instance."),
        ],
        *,
        algorithm: Annotated[
            Algorithm | None,
            Doc(
                """
                The rate-limit algorithm. Use
                [`TokenBucket`][grelmicro.resilience.algorithms.TokenBucket]
                or
                [`GCRA`][grelmicro.resilience.algorithms.GCRA].

                Required, unless you use the deprecated `limit`
                and `window` shortcut for GCRA. That shortcut
                will be removed in 0.15.0.
                """
            ),
        ] = None,
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
        fail_open: Annotated[
            bool,
            Doc(
                """
                When `True`, the rate limiter returns an allowed
                result if the backend raises an error, instead of
                re-raising the error.

                Use this for rate limiters where availability
                matters more than strict enforcement. For example,
                analytics events.
                """
            ),
        ] = False,
        limit: Annotated[
            PositiveInt | None,
            Doc(
                """
                Legacy shorthand for
                `algorithm=GCRA(limit=..., window=...)`.

                Pairs with `window`.
                """
            ),
            deprecated(
                "Use `algorithm=GCRA(limit=..., window=...)` instead. "
                "Will be removed in 0.15.0."
            ),
        ] = None,
        window: Annotated[
            PositiveFloat | None,
            Doc(
                """
                Legacy shorthand for
                `algorithm=GCRA(limit=..., window=...)`.

                Pairs with `limit`.
                """
            ),
            deprecated(
                "Use `algorithm=GCRA(limit=..., window=...)` instead. "
                "Will be removed in 0.15.0."
            ),
        ] = None,
    ) -> None:
        """Initialize the rate limiter."""
        resolved = _resolve_algorithm(algorithm, limit, window)
        self._config = RateLimiterConfig(name=name, algorithm=resolved)
        self._backend = backend or get_rate_limiter_backend()
        self._strategy: RateLimiterStrategy = self._backend.bind(resolved)
        self._fail_open = fail_open
        self._fallback = _build_fallback(resolved)

    @property
    def config(self) -> RateLimiterConfig:
        """Return the config."""
        return self._config

    def _log_fail_open(
        self,
        key: str,
        exc: Exception,
    ) -> None:
        """Log a fail-open warning for a backend error."""
        logger.warning(
            "Rate limiter '%s' backend error, failing open for key '%s'",
            self._config.name,
            key,
            exc_info=exc,
        )

    def _full_key(self, key: str) -> str:
        return f"{self._config.name}:{key}"

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
        """Check rate limit; raise if exceeded.

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


def _resolve_algorithm(
    algorithm: Algorithm | None,
    limit: PositiveInt | None,
    window: PositiveFloat | None,
) -> Algorithm:
    """Resolve the algorithm from explicit or legacy kwargs.

    Emits a `DeprecationWarning` when the legacy `limit` or
    `window` arguments are used. Raises `TypeError` for
    incompatible combinations.
    """
    legacy_used = limit is not None or window is not None
    if algorithm is not None:
        if legacy_used:
            msg = (
                "Pass either `algorithm=` or legacy `limit=`/`window=`, "
                "not both."
            )
            raise TypeError(msg)
        return algorithm
    if limit is not None and window is not None:
        warnings.warn(
            "RateLimiter(name, limit=..., window=...) is deprecated; "
            "use RateLimiter(name, algorithm=GCRA(limit=..., "
            "window=...)). Will be removed in 0.15.0.",
            DeprecationWarning,
            stacklevel=3,
        )
        return GCRA(limit=limit, window=window)
    if legacy_used:
        msg = (
            "Legacy `limit` and `window` must be provided together. "
            "Pass both, or migrate to "
            "`algorithm=GCRA(limit=..., window=...)`."
        )
        raise TypeError(msg)
    msg = (
        "RateLimiter requires `algorithm=` "
        "(or the deprecated `limit=` and `window=` shorthand for GCRA)."
    )
    raise TypeError(msg)


def _build_fallback(algorithm: Algorithm) -> RateLimitResult:
    """Build the fail-open fallback result for the given algorithm.

    Called once when
    [`RateLimiter`][grelmicro.resilience.RateLimiter] is
    created. The result is cached on the instance and reused
    on every fail-open path.
    """
    match algorithm:
        case TokenBucket():
            limit_value = algorithm.capacity
        case GCRA():
            limit_value = algorithm.limit
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
