from grelmicro.coordination import LeaderElection
from grelmicro.coordination.memory import MemoryLeaderElectionBackend
from grelmicro.task import Tasks

leader = LeaderElection("my-service", backend=MemoryLeaderElectionBackend())
task = Tasks()
task.add_task(leader)


@task.interval(seconds=60, leader=leader)
async def cleanup():
    print("Running cleanup...")
