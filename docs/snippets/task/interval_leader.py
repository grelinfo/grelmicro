from grelmicro.coordination import LeaderElection
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import Tasks

redis = RedisProvider("redis://localhost:6379/0")
leader = LeaderElection("my-service", backend=redis.leaderelection())
task = Tasks()
task.add_task(leader)


@task.every(seconds=60, leader=leader)
async def cleanup():
    print("Running cleanup...")
