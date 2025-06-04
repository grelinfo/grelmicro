from grelmicro.sync import LeaderElection
from grelmicro.task import TaskManager

leader = LeaderElection("my_task")
task = TaskManager()
task.add_task(leader)


@task.interval(seconds=5, sync=leader)
async def my_task():
    async with leader:
        print("Hello, World!")
