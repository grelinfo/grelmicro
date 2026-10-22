from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import Tasks

tasks = Tasks(timezone="Europe/Zurich")
redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, tasks])


@tasks.every(interval=60, gate="claim")
async def cleanup() -> None:
    print("One replica cleans up each minute")


@tasks.cron("0 2 * * *", gate="claim")
async def nightly_report() -> None:
    print("One replica builds the report at 02:00")


app = FastAPI()
micro.install(app)
