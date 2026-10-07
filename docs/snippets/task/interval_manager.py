from grelmicro.task import Tasks

tasks = Tasks()


@tasks.every(interval=5)
async def my_task():
    print("Hello, World!")
