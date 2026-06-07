from grelmicro.coordination import Lock
from grelmicro.task import Tasks

task = Tasks()


@task.interval(seconds=5)
async def my_task():
    async with Lock("shared-resource"):
        print("Hello, World!")
