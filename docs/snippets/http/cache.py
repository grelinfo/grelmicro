from fastapi import FastAPI
from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.http import CachedResponses, cache_response
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[Cache(redis), CachedResponses()])
app = FastAPI()

micro.install(app)


class Product(BaseModel):
    id: int
    name: str


@app.get("/products")
@cache_response(ttl=60)
async def list_products() -> list[Product]:
    return await load_products()


async def load_products() -> list[Product]:
    return [Product(id=1, name="Anvil")]
