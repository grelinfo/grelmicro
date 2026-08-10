from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.idempotency import Idempotency
from grelmicro.integrations.fastapi import IdempotencyMiddleware
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])
app = FastAPI()

micro.install(app)
app.add_middleware(
    IdempotencyMiddleware, idempotency=Idempotency("http", ttl=3600)
)


@app.post("/charge")
async def charge(amount: int) -> dict[str, int]:
    # A retry carrying the same Idempotency-Key replays this response.
    return {"amount": amount}
