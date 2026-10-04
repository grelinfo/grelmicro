from grelmicro.task import Tasks

tasks = Tasks(timezone="Europe/Zurich")


@tasks.cron("0 2 * * *", gate="claim")
async def nightly_report():
    print("Running the nightly report at 02:00 Zurich time")
