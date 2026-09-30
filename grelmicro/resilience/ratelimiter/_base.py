"""Shared base for rate-limiter algorithm configurations."""

from typing import Annotated

from pydantic import BaseModel
from typing_extensions import Doc

CLOCK_TOLERANCE = 1e-6
"""Seconds a sliding window forgives for float rounding.

A request this close to its slot is admitted, and `remaining` counts a
slot this close to opening.
"""


class _BaseRateLimiterConfig(BaseModel, frozen=True, extra="forbid"):
    """Common fields shared by every rate-limiter algorithm config.

    Concrete algorithm configs (`TokenBucketConfig`, `SlidingWindowConfig`)
    inherit from this base. Settings that apply to every variant,
    such as fail-open behaviour, live here so they round-trip with
    the config object.
    """

    fail_open: Annotated[
        bool,
        Doc(
            """
            When `True`, the rate limiter returns an allowed result
            if the backend raises an error, instead of re-raising.

            Use this for rate limiters where availability matters
            more than strict enforcement, for example analytics
            events. Default: `False`.
            """
        ),
    ] = False
