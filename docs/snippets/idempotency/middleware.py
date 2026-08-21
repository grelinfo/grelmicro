from fastapi import FastAPI
from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.http import IdempotentRequests
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, IdempotentRequests(ttl=3600)])
app = FastAPI()

micro.install(app)


class Charge(BaseModel):
    amount: int


@app.post("/charge")
async def charge(amount: int) -> Charge:
    # A retry carrying the same Idempotency-Key replays this response.
    return Charge(amount=amount)
