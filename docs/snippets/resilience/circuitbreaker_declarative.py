from datetime import timedelta

from grelmicro.resilience import CircuitBreaker, ConsecutiveCountConfig

config = ConsecutiveCountConfig(
    error_threshold=10,
    reset_timeout=timedelta(seconds=60),
    ignore_exceptions=(ValueError,),
)
cb = CircuitBreaker.from_config("payments", config)
