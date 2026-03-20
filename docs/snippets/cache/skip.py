from grelmicro.cache import TTLCache, cached

cache = TTLCache(maxsize=100, ttl=300)


@cached(cache, skip=lambda r: r is None)
async def find_user(user_id: int) -> dict | None:
    # Returns None if user not found — not cached
    return None
