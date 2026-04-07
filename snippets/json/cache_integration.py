from pydantic import BaseModel

from grelmicro.cache import PydanticSerializer, TTLCache
from grelmicro.cache.memory import MemoryCacheBackend


class User(BaseModel):
    id: int
    name: str


backend = MemoryCacheBackend()

cache = TTLCache[User](ttl=300, serializer=PydanticSerializer(User))


async def main() -> None:
    async with backend:
        await cache.set("user:1", User(id=1, name="Alice"))
        user = await cache.get("user:1")
        print(user)
