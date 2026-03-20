from grelmicro.cache import TTLCache, cached

cache = TTLCache(maxsize=100, ttl=300)


@cached(cache)
async def get_user(user_id: int) -> dict:
    return {"id": user_id}


async def main() -> None:
    await get_user(1)
    await get_user(1)

    info = get_user.cache_info()  # type: ignore[attr-defined]
    print(
        info
    )  # CacheInfo(hits=1, misses=1, maxsize=100, currsize=1, evictions=0)

    get_user.cache_clear()  # type: ignore[attr-defined]
