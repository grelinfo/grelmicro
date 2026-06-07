from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer, cached
from grelmicro.cache.memory import MemoryCacheAdapter

cache = Cache(MemoryCacheAdapter())
micro = Grelmicro(uses=[cache])

ttl_cache = cache.ttl(ttl=300, serializer=JsonSerializer())


@cached(ttl_cache, tags=["users", "user:{user_id}"])
async def get_user(user_id: int) -> dict:
    return {"id": user_id, "name": "Alice"}


async def update_user(user_id: int) -> None:
    # Drop only this user's cached entry.
    await ttl_cache.delete_tags(f"user:{user_id}")


async def reset_all_users() -> None:
    # Drop every cached user at once.
    await ttl_cache.delete_tags("users")


async def main() -> None:
    async with micro:
        await get_user(1)
        await update_user(1)
        await reset_all_users()
