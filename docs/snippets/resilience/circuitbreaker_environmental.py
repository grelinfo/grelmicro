from grelmicro.resilience import CircuitBreaker

# GREL_CIRCUIT_BREAKER_PAYMENTS_ERROR_THRESHOLD=10
# GREL_CIRCUIT_BREAKER_PAYMENTS_RESET_TIMEOUT=60
cb = CircuitBreaker("payments")
