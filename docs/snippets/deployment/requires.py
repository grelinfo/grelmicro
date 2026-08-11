from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimiterComponent

memory = MemoryProvider()
redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(
    uses=[
        # One replica owns this schedule, and a restart may repeat a run.
        Coordination(memory, name="cron", requires="process"),
        # A budget shared by every replica, or the app does not start.
        RateLimiterComponent(redis, requires="cluster"),
    ]
)
