"""Memory Rate Limiter Backend."""

import math
from time import monotonic
from types import TracebackType
from typing import Annotated, Self

from typing_extensions import Doc

from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience._protocol import RateLimiterBackend, RateLimitResult

_EVICTION_THRESHOLD = 1000


class MemoryRateLimiterBackend(RateLimiterBackend):
    """Memory Rate Limiter Backend.

    In-memory GCRA implementation for testing or single-process use.
    Stores one float (Theoretical Arrival Time) per key.
    """

    def __init__(
        self,
        *,
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register the rate limiter backend"
                " in the backend registry."
            ),
        ] = True,
    ) -> None:
        """Initialize the rate limiter backend."""
        self._tats: dict[str, float] = {}
        if auto_register:
            rate_limiter_backend_registry.set(self)

    async def __aenter__(self) -> Self:
        """Open the rate limiter backend."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the rate limiter backend."""
        self._tats.clear()

    async def acquire(
        self,
        *,
        key: str,
        limit: int,
        window: float,
        cost: int,
    ) -> RateLimitResult:
        """Try to acquire rate limit tokens using GCRA.

        Atomicity relies on no yield point between read
        and write of ``self._tats``.

        Args:
            key: The rate limit key.
            limit: Maximum requests allowed in the window.
            window: Window duration in seconds.
            cost: Number of tokens to consume.

        Returns:
            RateLimitResult with allowed, limit, remaining,
            retry_after, and reset_after fields.

        """
        now = monotonic()

        # Lazy eviction: keys with TAT < now behave identically to fresh
        # keys (max(tat, now) == now), so they can be safely removed.
        if len(self._tats) > _EVICTION_THRESHOLD:
            self._tats = {k: v for k, v in self._tats.items() if v >= now}

        emission_interval = window / limit
        increment = emission_interval * cost
        burst_offset = emission_interval * limit

        tat = self._tats.get(key, now)

        new_tat = max(tat, now) + increment
        allow_at = new_tat - burst_offset
        diff = now - allow_at
        remaining = math.floor(diff / emission_interval + 0.5)

        if remaining < 0:
            reset_after = tat - now
            retry_after = -diff
            return RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=0,
                retry_after=max(0.0, retry_after),
                reset_after=max(0.0, reset_after),
            )

        reset_after = new_tat - now
        self._tats[key] = new_tat
        return RateLimitResult(
            allowed=True,
            limit=limit,
            remaining=remaining,
            retry_after=0.0,
            reset_after=reset_after,
        )
