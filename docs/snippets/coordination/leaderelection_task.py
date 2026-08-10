from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import Tasks

task = Tasks()
redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, task])

leader = micro.coordination.leaderelection("cluster_group")
task.add_task(leader)
