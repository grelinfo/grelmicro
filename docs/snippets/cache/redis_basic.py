from pydantic import BaseModel

from grelmicro.cache import PydanticSerializer, TTLCache, cached
from grelmicro.cache.redis import RedisCacheAdapter


class User(BaseModel):
    id: int
    name: str


backend = RedisCacheAdapter(prefix="myapp:")

cache = TTLCache[User](ttl=300, serializer=PydanticSerializer(User))


@cached(cache, lock=True)
async def get_user(user_id: int) -> User:
    return User(id=user_id, name="Alice")


async def main() -> None:
    async with backend:
        user = await get_user(1)
        print(user)
