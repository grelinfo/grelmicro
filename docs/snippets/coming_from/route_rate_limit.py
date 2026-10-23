from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.http import ErrorResponses
from grelmicro.integrations.fastapi import RateLimited
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimiter
from grelmicro.security import TrustedProxies

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, ErrorResponses()])

app = FastAPI()
micro.install(app)

home = RateLimiter.sliding_window("home", limit=5, window=60)
proxies = TrustedProxies(["10.0.0.0/8"])


@app.get("/home", dependencies=[RateLimited(home, trusted=proxies)])
async def homepage() -> dict[str, str]:
    return {"message": "test"}
