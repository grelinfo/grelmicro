"""Type assertions for the resilience primitives."""

from typing import Any, assert_type

from grelmicro.resilience import (
    CircuitBreaker,
    RateLimiter,
    Retry,
    Shield,
)


async def fetch(order_id: str, *, retries: int = 3) -> dict[str, int]:
    """Fetch an order, pinning decorator behavior."""
    return {order_id: retries}


# --- Factories return the concrete policy, not a base or `Any` ---

assert_type(Retry.exponential("api", when=ValueError), Retry)
assert_type(Retry.constant("poll", when=ValueError), Retry)
assert_type(CircuitBreaker.consecutive_count("api"), CircuitBreaker)
assert_type(Shield.api("api"), Shield)
assert_type(Shield.internal("internal"), Shield)
assert_type(Shield.slow("slow"), Shield)
assert_type(
    RateLimiter.token_bucket("rl", capacity=10, refill_rate=1.0), RateLimiter
)
assert_type(RateLimiter.sliding_window("rl", limit=10, window=1.0), RateLimiter)


# --- Known gap: decorating erases the wrapped signature ---
#
# `Retry.__call__` and friends are typed `Callable[..., Awaitable[Any]]`, so a
# decorated function loses both its parameter types and its return type. These
# assertions pin the current behavior: when the decorators gain `ParamSpec`
# generics, they fail and must be tightened to the real types. See #543.

retried = Retry.exponential("api", when=ValueError)(fetch)
shielded = Shield.api("api")(fetch)


async def call_decorated() -> None:
    """Awaiting a decorated coroutine currently yields `Any`."""
    assert_type(await retried("ORD-1"), Any)
    assert_type(await shielded("ORD-1"), Any)
