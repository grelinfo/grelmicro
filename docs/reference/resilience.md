# Resilience

::: grelmicro.resilience
    options:
      show_submodules: true
      members:
        - Algorithm
        - CircuitBreaker
        - CircuitBreakerError
        - CircuitBreakerMetrics
        - CircuitBreakerState
        - ErrorDetails
        - GCRA
        - MemoryTokenBucket
        - RateLimiter
        - RateLimiterBackend
        - RateLimiterConfig
        - RateLimiterStrategy
        - RateLimitExceededError
        - RateLimitResult
        - ResilienceError
        - ResilienceSettingsValidationError
        - TokenBucket

::: grelmicro.resilience.memory
    options:
      members:
        - MemoryRateLimiterBackend

::: grelmicro.resilience.redis
    options:
      members:
        - RedisRateLimiterBackend
