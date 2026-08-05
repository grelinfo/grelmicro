from grelmicro.coordination import LeaderElection
from grelmicro.providers.memory import MemoryProvider
from grelmicro.task import Tasks

leader = LeaderElection("my-service", backend=MemoryProvider().leaderelection())
task = Tasks()
task.add_task(leader)


@task.every(seconds=60, leader=leader)
async def cleanup():
    print("Running cleanup...")
