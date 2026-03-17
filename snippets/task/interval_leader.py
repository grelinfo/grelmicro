from grelmicro.sync import LeaderElection
from grelmicro.task import TaskManager

leader = LeaderElection("my-service")
task = TaskManager()
task.add_task(leader)


@task.interval(seconds=60, leader=leader)
async def cleanup():
    print("Running cleanup...")
