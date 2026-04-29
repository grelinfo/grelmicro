from grelmicro.resilience import GCRAConfig, RateLimiter

cfg = GCRAConfig(limit=5, window=60)
limiter = RateLimiter.from_config("auth", cfg)
