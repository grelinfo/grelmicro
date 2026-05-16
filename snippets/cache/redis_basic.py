from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.cache import Cache, PydanticSerializer, cached
from grelmicro.providers.redis import RedisProvider


class User(BaseModel):
    id: int
    name: str


redis = RedisProvider("redis://localhost:6379/0")
cache = Cache(redis)
micro = Grelmicro(uses=[redis, cache])

ttl_cache = cache.ttl(ttl=300, serializer=PydanticSerializer(User))


@cached(ttl_cache, lock=True)
async def get_user(user_id: int) -> User:
    return User(id=user_id, name="Alice")


async def main() -> None:
    async with micro:
        user = await get_user(1)
        print(user)
