import os

from grelmicro import Grelmicro, Usable
from grelmicro.health import HealthChecks
from grelmicro.providers.redis import RedisProvider


def build_components() -> list[Usable]:
    components: list[Usable] = [HealthChecks()]
    if os.getenv("STORE_BACKEND") == "redis":
        components.append(RedisProvider("redis://localhost:6379/0"))
    return components


micro = Grelmicro(uses=build_components())
