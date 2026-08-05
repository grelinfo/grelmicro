import os

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.providers.redis import RedisProvider

health = HealthChecks()
redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(
    uses=[
        health,
        redis if os.getenv("STORE_BACKEND") == "redis" else None,
    ]
)
