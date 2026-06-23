from grelmicro.task import Tasks

task = Tasks()


@task.interval(seconds=60, lease_duration=300)
async def cleanup():
    print("Running cleanup...")
