from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience.redis import RedisRateLimiterBackend

backend = RedisRateLimiterBackend(
    provider=RedisProvider("redis://localhost:6379/0")
)
