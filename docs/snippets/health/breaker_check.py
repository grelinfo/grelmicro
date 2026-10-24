from grelmicro.health import HealthChecks
from grelmicro.resilience import CircuitBreaker

inverter = CircuitBreaker.consecutive_count("inverter", error_threshold=5)
heater = CircuitBreaker.consecutive_count("heater", error_threshold=5)

health = HealthChecks()

# Registered as "circuitbreaker:inverter", visible in /healthz only.
health.add_circuitbreaker(inverter)

# An open heater breaker fails /readyz.
health.add_circuitbreaker(heater, critical=True)
