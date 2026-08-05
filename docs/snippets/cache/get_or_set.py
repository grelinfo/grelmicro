from grelmicro import Grelmicro
from grelmicro.cache import JsonSerializer
from grelmicro.providers.memory import MemoryProvider

micro = Grelmicro(uses=[MemoryProvider()])

ttl_cache = micro.cache.ttl(ttl=300, serializer=JsonSerializer())


async def main() -> None:
    async with micro:
        # The factory runs only on a miss, then the value is cached.
        user = await ttl_cache.get_or_set(
            "user:1",
            lambda: {"id": 1, "name": "Alice"},
            tags=["users"],
        )
        print(user)
