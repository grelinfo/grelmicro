from grelmicro.coordination import Lock
from grelmicro.task import Tasks

task = Tasks()


@task.every(seconds=5)
async def my_task():
    async with Lock("shared-resource"):
        print("Hello, World!")
