"""Shield profile configuration base class.

Defines the public fields shared by every profile config and exposes
the profile-specific algorithm parameters as class variables. The
algorithm parameters are frozen by profile choice and never appear
as Pydantic fields.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc

from grelmicro.resilience._when import OutcomeFilter

__all__ = ["_BaseShieldConfig"]


class _BaseShieldConfig(BaseModel, frozen=True, extra="forbid"):
    """Base Shield configuration shared by every profile.

    Subclasses freeze the profile-specific algorithm parameters as
    `ClassVar` attributes and declare the `kind` literal for the
    discriminated union.
    """

    # --- Profile-frozen algorithm parameters (ClassVars) -----------------
    #
    # Subclasses set these. They are NOT Pydantic fields, so they never
    # appear in `model_dump()` and cannot be overridden per instance.

    max_consecutive_failures: ClassVar[int]
    initial_max_rate: ClassVar[float]
    adaptive_burst_capacity: ClassVar[float]
    min_rate_floor: ClassVar[float]
    initial_timeout: ClassVar[float]
    timeout_clamp_min: ClassVar[float]
    timeout_clamp_max: ClassVar[float]
    backoff_scale: ClassVar[float]
    backoff_cap: ClassVar[float]
    max_rate_cap_default: ClassVar[float | None] = None
    profile_name: ClassVar[str]

    # --- Public fields ---------------------------------------------------

    when: Annotated[
        OutcomeFilter,
        Doc(
            "Outcome filter naming the errors that count as transient. "
            "Pass a [`Match`][grelmicro.resilience.Match] or a shorthand: "
            "an exception class, a tuple of classes, or a predicate on "
            "the exception. A matching error is retried, shrinks the "
            "adaptive bucket, and consumes one retry-budget token. "
            "Anything else goes straight to recovery. `TimeoutError` "
            "always counts, whatever this filter says, because Shield's "
            "own per-attempt timeout raises it. Required."
        ),
    ]

    max_rate: Annotated[
        PositiveFloat | None,
        Doc(
            "Optional hard ceiling on the adaptive bucket's rate in "
            "tokens per second, per worker process. Four workers each "
            "hold this ceiling, so the dependency sees four times it. "
            "`None` disables the cap."
        ),
    ] = None

    cache: Annotated[
        Any,
        Doc(
            "Optional cache instance used as a fallback on give-up. "
            "Must expose `async def get(key) -> value | None` and "
            "`async def set(key, value)`. Values returned by the "
            "wrapped function are written fire-and-forget on success."
        ),
    ] = None

    cache_key: Annotated[
        Callable[..., str] | None,
        Doc(
            "Optional callable that returns the cache key for a call. "
            "Receives the same `(*args, **kwargs)` as the wrapped "
            'function. Defaults to `f"{name}:{stable_hash(args, kwargs)}"`.'
        ),
    ] = None

    fallback: Annotated[
        Callable[[BaseException], Any]
        | Callable[[BaseException], Awaitable[Any]]
        | None,
        Doc(
            "Optional callable invoked on give-up when the cache path "
            "does not return a value. Receives the underlying "
            "exception. May be sync or async."
        ),
    ] = None
