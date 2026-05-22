"""API Shield profile configuration.

Default profile. Tuned for external HTTP APIs with moderate latency,
occasional outages, and third-party SLAs.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from typing_extensions import Doc

from grelmicro.resilience.shield._profile import _BaseShieldConfig

__all__ = ["ApiShieldConfig"]


class ApiShieldConfig(_BaseShieldConfig, frozen=True, extra="forbid"):
    """Shield configuration for the `api` profile (the default).

    Tuned for external HTTP APIs. Modest initial rate, generous
    timeouts, broad clamps. The profile parameters are frozen.

    Read more in the [Shield](../resilience/shield.md) docs.
    """

    kind: Annotated[
        Literal["api"],
        Doc("Discriminator for the Shield profile Pydantic union."),
    ] = "api"

    max_consecutive_failures: ClassVar[int] = 20
    initial_max_rate: ClassVar[float] = 2.0
    adaptive_burst_capacity: ClassVar[float] = 5.0
    min_rate_floor: ClassVar[float] = 0.25
    initial_timeout: ClassVar[float] = 10.0
    timeout_clamp_min: ClassVar[float] = 0.5
    timeout_clamp_max: ClassVar[float] = 60.0
    backoff_scale: ClassVar[float] = 1.0
    backoff_cap: ClassVar[float] = 30.0
    max_rate_cap_default: ClassVar[float | None] = None
    profile_name: ClassVar[str] = "api"
