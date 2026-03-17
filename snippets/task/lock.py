from grelmicro.sync import Lock
from grelmicro.task import TaskManager

task = TaskManager()


@task.interval(seconds=5)
async def my_task():
    async with Lock("shared-resource"):
        print("Hello, World!")
