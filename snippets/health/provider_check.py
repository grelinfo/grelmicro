from grelmicro.health import HealthChecks
from grelmicro.providers.redis import RedisProvider

store = RedisProvider("redis://localhost:6379")
cache = RedisProvider("redis://localhost:6379/1")

health = HealthChecks()

# Register the provider's built-in readiness check as "provider:redis".
health.add_provider(store)

# A degradable dependency: visible in /healthz, never fails /readyz.
health.add_provider(cache, name="cache", critical=False)
