from grelmicro import Grelmicro
from grelmicro.cache import JsonSerializer, cached
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])

ttl_cache = micro.cache.ttl(ttl=300, serializer=JsonSerializer())


@cached(ttl_cache, key="user:{user_id}")
async def get_user(user_id: int) -> dict:
    return {"id": user_id, "name": "Alice"}


async def main() -> None:
    async with micro:
        # Keyed under "user:1" instead of the default argument-repr key.
        await get_user(1)
        await ttl_cache.delete("user:1")
