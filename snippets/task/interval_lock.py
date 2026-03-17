from grelmicro.task import TaskManager

task = TaskManager()


@task.interval(seconds=60, max_lock_seconds=300)
async def cleanup():
    print("Running cleanup...")
