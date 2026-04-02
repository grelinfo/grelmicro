"""Rate Limiter."""

from typing import Annotated

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro.resilience._backends import get_rate_limiter_backend
from grelmicro.resilience._protocol import RateLimitResult
from grelmicro.resilience.errors import RateLimitExceededError


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
    ) -> None:
        """Initialize the rate limiter."""
        self._config = RateLimiterConfig(
            name=name,
            limit=limit,
            window=window,
        )

    @property
    def config(self) -> RateLimiterConfig:
        """Return the config."""
        return self._config

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
        return await backend.acquire(
            key=full_key,
            limit=self._config.limit,
            window=self._config.window,
            cost=cost,
        )

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
