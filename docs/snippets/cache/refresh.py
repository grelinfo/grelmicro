from grelmicro import Grelmicro
from grelmicro.cache import JsonSerializer, TTLCache, cached
from grelmicro.providers.memory import MemoryProvider

micro = Grelmicro(uses=[MemoryProvider()])

cache = TTLCache(ttl=300, serializer=JsonSerializer())


@cached(cache, key="report:{user_id}")
async def get_report(user_id: int) -> dict:
    return {"user_id": user_id}


async def main() -> None:
    async with micro:
        await get_report(42)  # computed, then stored
        await get_report(42)  # served from the cache
        # Skips the read, recomputes, and overwrites the entry.
        await get_report.refresh(42)
