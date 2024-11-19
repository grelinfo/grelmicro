"""Example of a simple FastAPI app."""

from contextlib import asynccontextmanager

import typer
from fastapi import FastAPI

from grelmicro.backends.redis import RedisLockBackend
from grelmicro.sync import LeaderElection, Lock
from grelmicro.task import TaskManager


# === FastAPI ===
@asynccontextmanager
async def lifespan(app):
    # Start the lock backend and task manager
    async with lock_backend, task:
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    return {"Hello": "World"}


# === Grelmicro ===
task = TaskManager()
lock_backend = RedisLockBackend("redis://localhost:6379/0")

# --- Ensure that only one say hello world at the same time ---
lock = Lock("say_hello_world")


@task.interval(1, sync=lock)
def say_hello_world_every_second():
    typer.echo("Hello World")


@task.interval(1, sync=lock)
def say_as_well_hello_world_every_second():
    typer.echo("Hello World")


# --- Ensure that only one worker is the leader ---
leader_election = LeaderElection("leader-election")
task.add_task(leader_election)


@task.interval(10, sync=leader_election)
def say_hello_leader_every_ten_seconds():
    typer.echo("Hello Leader")
