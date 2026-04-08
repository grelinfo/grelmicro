"""Rate Limiter."""

import logging
from typing import Annotated

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro.resilience._backends import get_rate_limiter_backend
from grelmicro.resilience._protocol import RateLimitResult
from grelmicro.resilience.errors import RateLimitExceededError

logger = logging.getLogger(__name__)


class RateLimiterConfig(BaseModel, frozen=True, extra="forbid"):
    """Rate Limiter Config."""

    name: Annotated[
        str,
        Doc("The name of the rate limiter instance."),
    ]
    limit: Annotated[
        PositiveInt,
        Doc("Maximum number of requests per window."),
    ]
    window: Annotated[
        PositiveFloat,
        Doc("Window duration in seconds."),
    ]


class RateLimiter:
    """Rate limiter using Generic Cell Rate Algorithm (GCRA).

    Implements sliding-window rate limiting by tracking the
    theoretical arrival time of requests. Limits the number of
    calls within a time window per key. Requires a rate limiter
    backend (Redis or Memory).
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc("Name of the rate limiter instance."),
        ],
        *,
        limit: Annotated[
            PositiveInt,
            Doc("Maximum number of requests per window."),
        ],
        window: Annotated[
            PositiveFloat,
            Doc("Window duration in seconds."),
        ],
        fail_open: Annotated[
            bool,
            Doc(
                "When True, backend errors return an allowed result"
                " instead of propagating the exception."
                " Useful for non-critical rate limiters where"
                " availability is more important than strictness."
            ),
        ] = False,
    ) -> None:
        """Initialize the rate limiter."""
        self._config = RateLimiterConfig(
            name=name,
            limit=limit,
            window=window,
        )
        self._fail_open = fail_open

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

    def _allowed_result(self) -> RateLimitResult:
        """Return a fully-allowed result for fail-open fallback."""
        return RateLimitResult(
            allowed=True,
            limit=self._config.limit,
            remaining=self._config.limit,
            retry_after=0.0,
            reset_after=0.0,
        )

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
            ValueError: If cost is less than 1 or greater than limit.
        """
        if cost < 1 or cost > self._config.limit:
            msg = f"cost must be between 1 and {self._config.limit}, got {cost}"
            raise ValueError(msg)
        backend = get_rate_limiter_backend()
        full_key = f"{self._config.name}:{key}"
        try:
            return await backend.acquire(
                key=full_key,
                limit=self._config.limit,
                window=self._config.window,
                cost=cost,
            )
        except Exception as exc:
            if self._fail_open:
                self._log_fail_open(key, exc)
                return self._allowed_result()
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
            ``allowed`` indicates whether the next acquire would succeed.
        """
        backend = get_rate_limiter_backend()
        full_key = f"{self._config.name}:{key}"
        try:
            return await backend.peek(
                key=full_key,
                limit=self._config.limit,
                window=self._config.window,
            )
        except Exception as exc:
            if self._fail_open:
                self._log_fail_open(key, exc)
                return self._allowed_result()
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
        backend = get_rate_limiter_backend()
        full_key = f"{self._config.name}:{key}"
        try:
            await backend.reset(key=full_key)
        except Exception as exc:
            if self._fail_open:
                self._log_fail_open(key, exc)
                return
            raise
