from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.cache import Cache, PydanticSerializer, cached
from grelmicro.providers.postgres import PostgresProvider


class User(BaseModel):
    id: int
    name: str


postgres = PostgresProvider("postgresql://localhost:5432/app")
cache = Cache(postgres)
micro = Grelmicro(uses=[postgres, cache])

ttl_cache = cache.ttl(ttl=300, serializer=PydanticSerializer(User))


@cached(ttl_cache, stampede="local")
async def get_user(user_id: int) -> User:
    return User(id=user_id, name="Alice")


async def main() -> None:
    async with micro:
        user = await get_user(1)
        print(user)
