"""Shared base for rate-limiter algorithm configurations."""

from typing import Annotated

from pydantic import BaseModel
from typing_extensions import Doc


class _BaseRateLimiterConfig(BaseModel, frozen=True, extra="forbid"):
    """Common fields shared by every rate-limiter algorithm config.

    Concrete algorithm configs (`TokenBucketConfig`, `GCRAConfig`)
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
