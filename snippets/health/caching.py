from grelmicro.health import HealthChecks

# Default: 1-second TTL with single-flight per check
HealthChecks(timeout=5.0, cache_ttl=1.0)

# Disable caching entirely
HealthChecks(cache_ttl=0)
