from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer
from grelmicro.cache.memory import MemoryCacheAdapter

cache = Cache(MemoryCacheAdapter())
micro = Grelmicro(uses=[cache])

ttl_cache = cache.ttl(ttl=300, serializer=JsonSerializer())


async def main() -> None:
    async with micro:
        # Write many entries in one call.
        await ttl_cache.set_many(
            {"user:1": {"id": 1}, "user:2": {"id": 2}},
            tags=["users"],
        )

        # Read many keys at once. Missing keys are absent from the result.
        found = await ttl_cache.get_many(["user:1", "user:2", "user:3"])
        print(found)

        # Delete many keys in one call.
        await ttl_cache.delete_many(["user:1", "user:2"])
