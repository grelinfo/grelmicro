import asyncio
import time
from contextlib import asynccontextmanager
from typing import Annotated

import typer
from fast_depends import Depends
from fastapi import FastAPI

from grelmicro.coordination import LeaderElection, Lock
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
)
from grelmicro.task import Tasks

backend = MemoryLockAdapter()
coordination_backend = MemoryLeaderElectionAdapter()
task = Tasks()


@asynccontextmanager
async def lifespan(app):
    async with backend, coordination_backend, task:
        typer.echo("App started")
        yield
        typer.echo("App stopped")


app = FastAPI(lifespan=lifespan)

leased_lock_10sec = Lock(
    name="leased_lock_10sec",
    lease_duration=10,
    backend=backend,
)
leased_lock_5sec = Lock(
    name="leased_lock_5sec",
    lease_duration=5,
    backend=backend,
)

leader_election = LeaderElection(
    name="simple-leader", backend=coordination_backend
)

task.add_task(leader_election)


@task.every(seconds=1)
def sync_func_with_no_param():
    typer.echo("sync_with_no_param")


@task.every(seconds=2)
async def async_func_with_no_param():
    typer.echo("async_with_no_param")


def sync_dependency():
    return "sync_dependency"


@task.every(seconds=3)
def sync_func_with_sync_dependency(
    sync_dependency: Annotated[str, Depends(sync_dependency)],
):
    typer.echo(sync_dependency)


async def async_dependency():
    yield "async_with_async_dependency"


@task.every(seconds=4)
async def async_func_with_async_dependency(
    async_dependency: Annotated[str, Depends(async_dependency)],
):
    typer.echo(async_dependency)


@task.every(seconds=15, sync=leased_lock_10sec)
def sync_func_with_leased_lock_10sec():
    typer.echo("sync_func_with_leased_lock_10sec")
    time.sleep(9)


@task.every(seconds=15, sync=leased_lock_10sec)
async def async_func_with_leased_lock_10sec():
    typer.echo("async_func_with_leased_lock_10sec")
    await asyncio.sleep(9)


@task.every(seconds=15, sync=leased_lock_5sec)
def sync_func_with_sync_dependency_and_leased_lock_5sec(
    sync_dependency: Annotated[str, Depends(sync_dependency)],
):
    typer.echo(sync_dependency)
    time.sleep(4)


@task.every(seconds=15, sync=leased_lock_5sec)
async def async_func_with_async_dependency_and_leased_lock_5sec(
    async_dependency: Annotated[str, Depends(async_dependency)],
):
    typer.echo(async_dependency)
    await asyncio.sleep(4)


@task.every(seconds=15, sync=leader_election)
def sync_func_with_leader_election():
    typer.echo("sync_func_with_leader_election")
    time.sleep(30)


@task.every(seconds=15, sync=leader_election)
async def async_func_with_leader_election():
    typer.echo("async_func_with_leader_election")
    await asyncio.sleep(30)
