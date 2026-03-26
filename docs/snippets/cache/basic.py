from grelmicro.cache import TTLCache, cached

cache = TTLCache(maxsize=100, ttl=300)


@cached(cache)
async def get_user(user_id: int) -> dict:
    # Expensive operation (e.g., database query)
    return {"id": user_id, "name": "Alice"}


async def main() -> None:
    user = await get_user(1)  # cache miss: calls function
    user = await get_user(1)  # cache hit: returns cached result
    print(user)
