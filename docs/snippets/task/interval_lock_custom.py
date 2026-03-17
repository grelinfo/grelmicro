from grelmicro.task import TaskManager

task = TaskManager()


@task.interval(seconds=60, max_lock_seconds=300, min_lock_seconds=30)
async def long_task():
    print("Running long task...")
