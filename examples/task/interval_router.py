from grelmicro.task import TaskRouter

task = TaskRouter()


@task.interval(seconds=5)
async def my_task():
    print("Hello, World!")
