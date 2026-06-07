from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer
from grelmicro.cache.memory import MemoryCacheAdapter

cache = Cache(MemoryCacheAdapter())
micro = Grelmicro(uses=[cache])

ttl_cache = cache.ttl(ttl=300, serializer=JsonSerializer())


async def main() -> None:
    async with micro:
        # The factory runs only on a miss, then the value is cached.
        user = await ttl_cache.get_or_set(
            "user:1",
            lambda: {"id": 1, "name": "Alice"},
            tags=["users"],
        )
        print(user)
