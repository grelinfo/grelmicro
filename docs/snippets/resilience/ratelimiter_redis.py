from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimiterRegistry

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[RateLimiterRegistry(redis)])
