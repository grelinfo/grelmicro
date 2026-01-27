from contextlib import asynccontextmanager

import typer
from fastapi import FastAPI

from grelmicro.logging.loguru import configure_logging
from grelmicro.sync import LeaderElection, Lock
from grelmicro.sync.redis import RedisSyncBackend
from grelmicro.task import TaskManager


# === FastAPI ===
@asynccontextmanager
async def lifespan(app):
    configure_logging()
    # Start the lock backend and task manager
    async with sync_backend, task:
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    return {"Hello": "World"}


# === Grelmicro ===
task = TaskManager()
sync_backend = RedisSyncBackend("redis://localhost:6379/0")

# --- Ensure that only one say hello world at the same time ---
lock = Lock("say_hello_world")


@task.interval(seconds=1, sync=lock)
def say_hello_world_every_second():
    typer.echo("Hello World")


@task.interval(seconds=1, sync=lock)
def say_as_well_hello_world_every_second():
    typer.echo("Hello World")


# --- Ensure that only one worker is the leader ---
leader_election = LeaderElection("leader-election")
task.add_task(leader_election)


@task.interval(seconds=10, sync=leader_election)
def say_hello_leader_every_ten_seconds():
    typer.echo("Hello Leader")
