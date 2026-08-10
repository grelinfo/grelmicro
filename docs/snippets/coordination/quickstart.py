from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import Tasks

tasks = Tasks()
redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, tasks])

leader = micro.coordination.leaderelection("worker")
tasks.add_task(leader)


@tasks.every(seconds=10, leader=leader)
async def run_once_in_the_cluster() -> None:
    print("only the leader runs this")
