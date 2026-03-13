from grelmicro.task import TaskManager

task = TaskManager()


@task.scheduled(seconds=60, lock_at_most_for=300)
async def long_task():
    print("Running long task...")
