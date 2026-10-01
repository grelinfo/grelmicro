from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.http import ErrorResponses, RateLimitedRequests
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimiter, RateLimiterComponent
from grelmicro.security import TrustedProxies

redis = RedisProvider("redis://localhost:6379/0")

burst = RateLimiter.sliding_window("burst", limit=100, window=60)
flood = RateLimiter.sliding_window("flood", limit=600, window=60)

micro = Grelmicro(
    uses=[
        RateLimiterComponent(redis),
        ErrorResponses(),
        RateLimitedRequests(
            burst,
            flood=flood,
            trusted=TrustedProxies(["10.0.0.0/8"]),
            exclude=("/livez", "/readyz"),
        ),
    ]
)
app = FastAPI()

micro.install(app)
