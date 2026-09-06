from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.http import CachedResponses
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(
    uses=[
        Cache(redis),
        CachedResponses(include={"/catalog": 300, "/products/*": 60}),
    ]
)
app = FastAPI()

micro.install(app)
