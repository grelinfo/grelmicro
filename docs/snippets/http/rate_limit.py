from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.http import ErrorResponses, RateLimitedRequests
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimiter, RateLimiterComponent
from grelmicro.security import TrustedProxies

redis = RedisProvider("redis://localhost:6379/0")

burst = RateLimiter.sliding_window("burst", limit=100, window=60)
daily = RateLimiter.sliding_window("daily", limit=10000, window=86400)

micro = Grelmicro(
    uses=[
        RateLimiterComponent(redis),
        ErrorResponses(),
        RateLimitedRequests(
            burst,
            daily,
            trusted=TrustedProxies(["10.0.0.0/8"]),
            exclude=("/livez", "/readyz"),
        ),
    ]
)
app = FastAPI()

micro.install(app)
