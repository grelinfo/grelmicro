"""Sliding-window rate-limiter configuration."""

from typing import Annotated, Literal, Self

from pydantic import PositiveInt, model_validator
from typing_extensions import Doc

from grelmicro._duration import Duration
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
        Duration,
        Doc(
            """
            Window duration, in whole seconds or as a `timedelta`.

            A float is refused. Use a `timedelta` for a window under a
            second, such as `timedelta(milliseconds=500)`. From text,
            such as an environment variable, it reads whole seconds
            (`"60"`) or a duration such as ISO 8601 `"PT0.5S"`. A
            decimal number of seconds, such as `"1.5"`, is refused.

            Each request gets `window / limit`, truncated to the
            microsecond, and must get at least one microsecond. The
            window is at most 100 years.
            """
        ),
    ]

    @model_validator(mode="after")
    def _check_slot(self) -> Self:
        """Refuse a window under a microsecond a request."""
        if _gcra.window_microseconds(self.window) < self.limit:
            msg = "window / limit must be at least one microsecond"
            raise ValueError(msg)
        return self
