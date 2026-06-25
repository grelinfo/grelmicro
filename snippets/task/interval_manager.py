from grelmicro.task import Tasks

task = Tasks()


@task.every(seconds=5)
async def my_task():
    print("Hello, World!")
