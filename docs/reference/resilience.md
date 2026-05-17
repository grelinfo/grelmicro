# Resilience

::: grelmicro.resilience
    options:
      show_submodules: true
      members:
        - Breaker
        - CircuitBreaker
        - CircuitBreakerBackend
        - CircuitBreakerConfig
        - CircuitBreakerError
        - CircuitBreakerMetrics
        - CircuitBreakerSharedState
        - CircuitBreakerState
        - ConstantBackoff
        - ErrorDetails
        - ExponentialBackoff
        - FibonacciBackoff
        - LinearBackoff
        - Match
        - Matcher
        - MemoryTokenBucket
        - Outcome
        - RandomBackoff
        - RateLimit
        - RateLimiter
        - RateLimiterBackend
        - RateLimiterConfig
        - RateLimiterStrategy
        - RateLimitExceededError
        - RateLimitResult
        - ResilienceError
        - ResilienceSettingsValidationError
        - Retry
        - RetryAttempt
        - RetryBackoffConfig
        - RetryConfig
        - RetryStrategy
        - SlidingWindowConfig
        - TokenBucketConfig
        - retry
        - retrying

::: grelmicro.resilience.memory
    options:
      members:
        - MemoryRateLimiterAdapter

::: grelmicro.resilience.redis
    options:
      members:
        - RedisCircuitBreakerAdapter
        - RedisRateLimiterAdapter
