from grelmicro.task import Tasks

task = Tasks()


@task.cron("*/5 * * * *", gate="claim")
async def sync_data():
    print("Syncing on one worker")
