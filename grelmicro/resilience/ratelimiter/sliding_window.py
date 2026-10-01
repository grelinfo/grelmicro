"""Sliding-window rate-limiter configuration."""

from datetime import timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import PositiveInt, field_validator, model_validator
from typing_extensions import Doc

from grelmicro.resilience.ratelimiter import _gcra
from grelmicro.resilience.ratelimiter._base import _BaseRateLimiterConfig


class SlidingWindowConfig(_BaseRateLimiterConfig, frozen=True, extra="forbid"):
    """Precise sliding-window rate limiting.

    Stores a single timestamp per key (about 72 bytes).

    Use this when you need a precise sliding window, such as for
    HTTP API throttling with the IETF `RateLimit` headers or the
    legacy `X-RateLimit-*` headers. For the pattern "allow a burst
    of N, then 1 per second", use
    [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]
    instead.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, SlidingWindowConfig

    # 5 requests per 60-second sliding window.
    rl = RateLimiter.from_config("auth", SlidingWindowConfig(limit=5, window=60))
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    kind: Annotated[
        Literal["sliding_window"],
        Doc("Discriminator for the algorithm Pydantic union."),
    ] = "sliding_window"

    limit: Annotated[
        PositiveInt,
        Doc("Maximum number of requests allowed per window."),
    ]

    window: Annotated[
        int | timedelta,
        Doc(
            """
            Window duration, in whole seconds or as a `timedelta`.

            A float is refused. Use a `timedelta` for a window under a
            second, such as `timedelta(milliseconds=500)`. From text,
            such as an environment variable, it reads whole seconds
            (`"60"`) or an ISO 8601 duration (`"PT0.5S"`).

            Each request gets `window / limit`, truncated to the
            microsecond, and must get at least one microsecond. The
            window is at most 100 years.
            """
        ),
    ]

    @field_validator("window", mode="before")
    @classmethod
    def _refuse_float(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a float or a bool before Pydantic converts it."""
        if isinstance(value, (bool, float)):
            msg = "window must be whole seconds or a timedelta"
            raise ValueError(msg)  # noqa: TRY004
        return value

    @model_validator(mode="after")
    def _check_slot(self) -> Self:
        """Refuse a window out of range, or under a microsecond a request."""
        window = _gcra.window_microseconds(self.window)
        if window <= 0:
            msg = "window must be greater than zero"
            raise ValueError(msg)
        if window > _gcra.window_microseconds(_gcra.MAX_WINDOW):
            msg = "window must be at most 100 years"
            raise ValueError(msg)
        if window < self.limit:
            msg = "window / limit must be at least one microsecond"
            raise ValueError(msg)
        return self
