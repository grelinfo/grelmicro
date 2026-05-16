from grelmicro.resilience import RateLimiter, SlidingWindowConfig

cfg = SlidingWindowConfig(limit=5, window=60)
limiter = RateLimiter.from_config("auth", cfg)
