from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience.redis import RedisRateLimiterAdapter

backend = RedisRateLimiterAdapter(
    provider=RedisProvider("redis://localhost:6379/0")
)
