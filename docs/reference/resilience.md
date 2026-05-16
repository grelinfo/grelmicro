# Resilience

::: grelmicro.resilience
    options:
      show_submodules: true
      members:
        - CircuitBreaker
        - CircuitBreakerError
        - CircuitBreakerMetrics
        - CircuitBreakerState
        - ErrorDetails
        - MemoryTokenBucket
        - RateLimiter
        - RateLimiterBackend
        - RateLimiterConfig
        - RateLimiterStrategy
        - RateLimitExceededError
        - RateLimitResult
        - ResilienceError
        - ResilienceSettingsValidationError
        - SlidingWindowConfig
        - TokenBucketConfig

::: grelmicro.resilience.memory
    options:
      members:
        - MemoryRateLimiterAdapter

::: grelmicro.resilience.redis
    options:
      members:
        - RedisRateLimiterAdapter
