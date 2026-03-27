import json

from grelmicro.cache import TTLCache, cached
from grelmicro.cache.redis import RedisCacheBackend

backend = RedisCacheBackend(prefix="myapp:")

cache = TTLCache(
    ttl=300,
    serializer=lambda v: json.dumps(v).encode(),
    deserializer=json.loads,
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
