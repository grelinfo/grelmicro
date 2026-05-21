"""Slow Shield profile configuration.

For long-running calls: LLM inference, batch jobs, large queries.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from typing_extensions import Doc

from grelmicro.resilience.shield._profile import _BaseShieldConfig

__all__ = ["SlowShieldConfig"]


class SlowShieldConfig(_BaseShieldConfig, frozen=True, extra="forbid"):
    """Shield configuration for the `slow` profile.

    Tuned for long-running calls: LLM inference, batch jobs, large
    queries. Low initial rate, large timeouts, tight failure budget.
    The profile parameters are frozen.

    Read more in the [Shield](../resilience/shield.md) docs.
    """

    kind: Annotated[
        Literal["slow"],
        Doc("Discriminator for the Shield profile Pydantic union."),
    ] = "slow"

    max_consecutive_failures: ClassVar[int] = 5
    initial_max_rate: ClassVar[float] = 0.5
    adaptive_burst_capacity: ClassVar[float] = 1.0
    min_rate_floor: ClassVar[float] = 0.05
    initial_timeout: ClassVar[float] = 120.0
    timeout_clamp_min: ClassVar[float] = 5.0
    timeout_clamp_max: ClassVar[float] = 600.0
    backoff_scale: ClassVar[float] = 2.0
    backoff_cap: ClassVar[float] = 60.0
    max_rate_cap_default: ClassVar[float | None] = None
    profile_name: ClassVar[str] = "slow"
