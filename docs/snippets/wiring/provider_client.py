from typing import Any

from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(uses=[redis])


async def save_order(order_id: str, total: str) -> None:
    await redis.client.hset(f"order:{order_id}", mapping={"total": total})


async def load_order(order_id: str) -> dict[str, Any]:
    return await redis.client.hgetall(f"order:{order_id}")
