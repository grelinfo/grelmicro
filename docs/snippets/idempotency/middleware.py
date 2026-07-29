from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.idempotency import Idempotency
from grelmicro.integrations.fastapi import IdempotencyMiddleware

micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
app = FastAPI()

app.add_middleware(
    IdempotencyMiddleware, idempotency=Idempotency("http", ttl=3600)
)
# Install last, so the grelmicro request scope wraps the middleware.
micro.install(app)


@app.post("/charge")
async def charge(amount: int) -> dict[str, int]:
    # A retry carrying the same Idempotency-Key replays this response.
    return {"amount": amount}
