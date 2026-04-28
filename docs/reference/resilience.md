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
        - GCRAConfig
        - MemoryTokenBucket
        - RateLimiter
        - RateLimiterBackend
        - RateLimiterConfig
        - RateLimiterStrategy
        - RateLimitExceededError
        - RateLimitResult
        - ResilienceError
        - ResilienceSettingsValidationError
        - TokenBucketConfig

::: grelmicro.resilience.memory
    options:
      members:
        - MemoryRateLimiterBackend

::: grelmicro.resilience.redis
    options:
      members:
        - RedisRateLimiterBackend
