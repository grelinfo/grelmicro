from grelmicro.task import TaskManager

task = TaskManager()


@task.interval(seconds=60, lock_at_most_for=300, lock_at_least_for=30)
async def long_task():
    print("Running long task...")
