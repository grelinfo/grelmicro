from grelmicro.coordination import LeaderElection
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import Tasks

redis = RedisProvider("redis://localhost:6379/0")
leader = LeaderElection("my-service", backend=redis.leaderelection())
tasks = Tasks()
tasks.add_task(leader)


@tasks.every(interval=60, gate=leader)
async def cleanup():
    print("Running cleanup...")
