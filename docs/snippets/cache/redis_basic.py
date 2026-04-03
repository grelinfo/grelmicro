from grelmicro.cache import TTLCache, cached
from grelmicro.cache.redis import RedisCacheBackend
from grelmicro.json import json_dumps_bytes, json_loads_bytes

backend = RedisCacheBackend(prefix="myapp:")  # app-level isolation

cache = TTLCache(
    ttl=300,
    serializer=json_dumps_bytes,
    deserializer=json_loads_bytes,
)


@cached(cache, lock=True)
async def get_user(user_id: int) -> dict:
    # Expensive database query
    return {"id": user_id, "name": "Alice"}


async def main() -> None:
    async with backend:
        user = await get_user(1)  # cache miss: calls function
        user = await get_user(1)  # cache hit: returns cached result
        print(user)
