import asyncio

from grelmicro.cache import TTLCache, cached

cache = TTLCache(maxsize=100, ttl=300)


@cached(cache, lock=asyncio.Lock())
async def fetch_expensive(key: str) -> dict:
    # Only one caller recomputes on cache miss;
    # others wait for the result.
    return {"key": key}
