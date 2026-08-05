from grelmicro import Grelmicro
from grelmicro.providers.memory import MemoryProvider
from grelmicro.task import Tasks

tasks = Tasks()
micro = Grelmicro(uses=[MemoryProvider(), tasks])

leader = micro.coordination.leaderelection("worker")
tasks.add_task(leader)


@tasks.every(seconds=10, leader=leader)
async def run_once_in_the_cluster() -> None:
    print("only the leader runs this")
