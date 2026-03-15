from grelmicro.task import TaskManager

task = TaskManager()


@task.interval(seconds=60, lock_at_most_for=300)
async def cleanup():
    print("Running cleanup...")
