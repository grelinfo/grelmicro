import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro.logging import configure_logging
from grelmicro.resilience.circuitbreaker import CircuitBreaker
from grelmicro.sync import LeaderElection, Lock
from grelmicro.sync.redis import RedisSyncBackend
from grelmicro.task import TaskManager

logger = logging.getLogger(__name__)

# === grelmicro ===
task = TaskManager()
sync_backend = RedisSyncBackend("redis://localhost:6379/0")
leader_election = LeaderElection("leader-election")
task.add_task(leader_election)


# === FastAPI ===
@asynccontextmanager
async def lifespan(app):
    configure_logging()
    async with sync_backend, task:
        yield


app = FastAPI(lifespan=lifespan)


# --- Circuit Breaker: protect calls to an unreliable service ---
cb = CircuitBreaker("my-service")


@app.get("/")
async def read_root():
    async with cb:
        return {"Hello": "World"}


# --- Distributed Lock: synchronize access to a shared resource ---
lock = Lock("shared-resource")


@app.get("/protected")
async def protected():
    async with lock:
        return {"status": "ok"}


# --- Interval Task: run locally on every worker ---
@task.interval(seconds=5)
def heartbeat():
    logger.info("heartbeat")


# --- Scheduled Task: run once per interval across all workers ---
@task.scheduled(seconds=60)
def cleanup():
    logger.info("cleanup")


# --- Leader-gated Scheduled Task: only the leader executes ---
@task.scheduled(seconds=10, leader=leader_election)
def leader_only_task():
    logger.info("leader task")
