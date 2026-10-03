from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import Tasks

tasks = Tasks()
redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, tasks])

leader = micro.coordination.leaderelection("cluster_group")
tasks.add_task(leader)
