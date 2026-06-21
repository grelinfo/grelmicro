from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379")
postgres = PostgresProvider("postgresql://localhost:5432/app")

# Registers "provider:redis" and "provider:postgres" critical checks on startup.
micro = Grelmicro(uses=[redis, postgres, HealthChecks(auto_health=True)])
