from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.cache import TTLCache, cached
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])


class Report(BaseModel):
    user_id: int


cache = TTLCache[Report](ttl=300)


@cached(cache, key="report:{user_id}")
async def get_report(user_id: int) -> Report:
    return Report(user_id=user_id)


async def main() -> None:
    async with micro:
        await get_report(42)  # computed, then stored
        await get_report(42)  # served from the cache
        # Skips the read, recomputes, and overwrites the entry.
        await get_report.refresh(42)
