from grelmicro.resilience import RateLimiter

auth_limiter = RateLimiter.gcra("auth", limit=5, window=60)
api_limiter = RateLimiter.token_bucket(
    "api",
    capacity=100,
    refill_rate=10,
)
