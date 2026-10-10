from datetime import timedelta

from grelmicro.resilience import RateLimiter, SlidingWindowConfig

cfg = SlidingWindowConfig(limit=5, window=timedelta(minutes=1))
limiter = RateLimiter.from_config("auth", cfg)
