import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.log import configure
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakers
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.sync import LeaderElection, Lock, Sync
from grelmicro.task import Tasks

logger = logging.getLogger(__name__)

# === grelmicro ===
tasks = Tasks()
leader_election = LeaderElection("leader-election")
tasks.add_task(leader_election)

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(
    uses=[
        redis,
        Sync(redis),
        CircuitBreakers(MemoryCircuitBreakerAdapter()),
        tasks,
    ]
)


# === FastAPI ===
@asynccontextmanager
async def lifespan(app):
    configure()
    async with micro:
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
@tasks.interval(seconds=5)
def heartbeat():
    logger.info("heartbeat")


# --- Distributed Task: run once per interval across all workers ---
@tasks.interval(seconds=60, max_lock_seconds=300)
def cleanup():
    logger.info("cleanup")


# --- Leader-gated Task: only the leader executes ---
@tasks.interval(seconds=10, leader=leader_election)
def leader_only_task():
    logger.info("leader task")
