from grelmicro import Grelmicro
from grelmicro.providers.memory import MemoryProvider
from grelmicro.task import Tasks

task = Tasks()
micro = Grelmicro(uses=[MemoryProvider(), task])

leader = micro.coordination.leaderelection("cluster_group")
task.add_task(leader)
