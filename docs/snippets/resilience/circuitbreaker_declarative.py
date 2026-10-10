from datetime import timedelta

from grelmicro.resilience import CircuitBreaker, ConsecutiveCountConfig, Match

config = ConsecutiveCountConfig(
    error_threshold=10,
    reset_timeout=timedelta(seconds=60),
    when=Match.not_exception(ValueError),
)
cb = CircuitBreaker.from_config("payments", config)
