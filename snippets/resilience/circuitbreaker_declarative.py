from grelmicro.resilience import CircuitBreaker, ConsecutiveCountConfig

config = ConsecutiveCountConfig(
    error_threshold=10,
    reset_timeout=60.0,
    ignore_exceptions=(ValueError,),
)
cb = CircuitBreaker.from_config("payments", config)
