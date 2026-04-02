from grelmicro.resilience.redis import RedisRateLimiterBackend

backend = RedisRateLimiterBackend("redis://localhost:6379/0")
