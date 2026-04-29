from grelmicro.resilience import CircuitBreaker, CircuitBreakerConfig

config = CircuitBreakerConfig(
    error_threshold=10,
    reset_timeout=60.0,
    ignore_exceptions=(ValueError,),
)
cb = CircuitBreaker.from_config("payments", config)
