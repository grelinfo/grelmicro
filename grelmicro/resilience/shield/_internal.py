"""Internal Shield profile configuration.

For in-cluster RPC. Healthy services, fast latency, tight budgets.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from typing_extensions import Doc

from grelmicro.resilience.shield._profile import _BaseShieldConfig

__all__ = ["InternalShieldConfig"]


class InternalShieldConfig(_BaseShieldConfig, frozen=True, extra="forbid"):
    """Shield configuration for the `internal` profile.

    Tuned for in-cluster RPC. High initial rate, short timeouts, tight
    budgets. The profile parameters are frozen.

    Read more in the [Shield](../resilience/shield.md) docs.
    """

    kind: Annotated[
        Literal["internal"],
        Doc("Discriminator for the Shield profile Pydantic union."),
    ] = "internal"

    max_consecutive_failures: ClassVar[int] = 10
    initial_max_rate: ClassVar[float] = 100.0
    adaptive_burst_capacity: ClassVar[float] = 200.0
    min_rate_floor: ClassVar[float] = 1.0
    initial_timeout: ClassVar[float] = 1.0
    timeout_clamp_min: ClassVar[float] = 0.05
    timeout_clamp_max: ClassVar[float] = 5.0
    backoff_scale: ClassVar[float] = 0.5
    backoff_cap: ClassVar[float] = 5.0
    max_rate_cap_default: ClassVar[float | None] = None
    profile_name: ClassVar[str] = "internal"
