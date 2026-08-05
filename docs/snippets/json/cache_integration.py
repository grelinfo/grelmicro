from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.cache import PydanticSerializer, TTLCache
from grelmicro.providers.memory import MemoryProvider


class User(BaseModel):
    id: int
    name: str


micro = Grelmicro(uses=[MemoryProvider()])

cache = TTLCache[User](ttl=300, serializer=PydanticSerializer(User))


async def main() -> None:
    async with micro:
        await cache.set("user:1", User(id=1, name="Alice"))
        user = await cache.get("user:1")
        print(user)
