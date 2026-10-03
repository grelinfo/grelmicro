from grelmicro.task import Tasks

tasks = Tasks()


@tasks.cron("*/5 * * * *", gate="claim")
async def sync_data():
    print("Syncing on one worker")
