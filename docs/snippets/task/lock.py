from grelmicro.sync import Lock
from grelmicro.task import TaskManager

lock = Lock("my_task")
task = TaskManager()


@task.interval(seconds=5, sync=lock)
async def my_task():
    async with lock:
        print("Hello, World!")
