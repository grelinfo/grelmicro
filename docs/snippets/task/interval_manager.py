from grelmicro.task import Tasks

task = Tasks()


@task.interval(seconds=5)
async def my_task():
    print("Hello, World!")
