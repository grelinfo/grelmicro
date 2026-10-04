from grelmicro.task import Tasks

tasks = Tasks()


@tasks.every(seconds=5)
async def my_task():
    print("Hello, World!")
