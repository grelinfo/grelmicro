from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.http import ErrorResponses
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimiter, RateLimiterComponent

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, RateLimiterComponent(redis), ErrorResponses()])
app = FastAPI()

micro.install(app)

limiter = RateLimiter.sliding_window("api", limit=100, window=60)


@app.get("/quote")
async def quote(client: str) -> dict[str, int]:
    # Over budget raises RateLimitExceededError, which leaves the handler
    # as a 429 problem detail carrying Retry-After.
    await limiter.acquire_or_raise(key=client)
    return {"price": 42}
