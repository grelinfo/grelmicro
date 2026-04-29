from grelmicro.resilience import CircuitBreaker

cb = CircuitBreaker(
    "payments",
    error_threshold=5,
    success_threshold=2,
    reset_timeout=30,
)
