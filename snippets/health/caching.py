from grelmicro.health import HealthRegistry

# Default: 1-second TTL with single-flight per check
HealthRegistry(timeout=5.0, cache_ttl=1.0)

# Disable caching entirely
HealthRegistry(cache_ttl=0)
