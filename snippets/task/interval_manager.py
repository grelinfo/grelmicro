from grelmicro.task import TaskManager

task = TaskManager()


@task.interval(seconds=5)
async def my_task():
    print("Hello, World!")
