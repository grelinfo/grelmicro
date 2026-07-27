"""Type assertions for the cache decorators.

`@cached` preserves the wrapped signature, so a cached call stays checked
against the original parameters and return type.
"""

from typing import assert_type

from grelmicro.cache import TTLCache, cached


async def load(user_id: int, *, fresh: bool = False) -> list[str]:
    """Load values, pinning decorator signature preservation."""
    return [str(user_id), str(fresh)]


memoized = cached(ttl=60)(load)


async def call_cached() -> None:
    """Awaiting a cached coroutine yields the original return type."""
    assert_type(await memoized(1), list[str])
    assert_type(await memoized(1, fresh=True), list[str])


async def use_ttl_cache() -> None:
    """`TTLCache` reads return the stored type."""
    cache: TTLCache[str] = TTLCache(maxsize=10, ttl=60)
    assert_type(await cache.get("k"), str | None)
