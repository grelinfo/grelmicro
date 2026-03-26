import json

from grelmicro.cache import cached
from grelmicro.cache.redis import RedisCache

cache = RedisCache(prefix="myapp:", ttl=300)


@cached(
    cache,
    lock=True,
    serializer=lambda v: json.dumps(v).encode(),
    deserializer=json.loads,
)
async def get_user(user_id: int) -> dict:
    # Expensive database query
    return {"id": user_id, "name": "Alice"}


async def main() -> None:
    async with cache:
        user = await get_user(1)  # cache miss: calls function
        user = await get_user(1)  # cache hit: returns cached result
        print(user)
