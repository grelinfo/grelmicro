from grelmicro.coordination import LeaderElection
from grelmicro.providers.memory import MemoryProvider
from grelmicro.task import Tasks

leader = LeaderElection("my-service", backend=MemoryProvider().leaderelection())
task = Tasks()
task.add_task(leader)


@task.every(seconds=5)
async def my_task():
    if leader.is_leader():
        print("Hello from the leader!")
