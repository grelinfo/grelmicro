import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, LeaderElection
from grelmicro.coordination.memory import MemoryLeaderElectionBackend
from grelmicro.log import configure
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakers
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.sync import Lock, Sync
from grelmicro.task import Tasks

logger = logging.getLogger(__name__)

# === grelmicro ===
tasks = Tasks()
coordination_backend = MemoryLeaderElectionBackend()
leader_election = LeaderElection(
    "leader-election", backend=coordination_backend
)
tasks.add_task(leader_election)

redis = RedisProvider("redis://localhost:6379/0")

# Patterns used inside FastAPI request handlers take an explicit backend:
# handlers run in their own task, outside the app's ambient scope. Background
# tasks run inside that scope, so they resolve their backend ambiently.
sync_backend = redis.sync()
breaker_backend = MemoryCircuitBreakerAdapter()

micro = Grelmicro(
    uses=[
        redis,
        Sync(sync_backend),
        Coordination(coordination_backend),
        CircuitBreakers(breaker_backend),
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
cb = CircuitBreaker("my-service", backend=breaker_backend)


@app.get("/")
async def read_root():
    async with cb:
        return {"Hello": "World"}


# --- Distributed Lock: synchronize access to a shared resource ---
lock = Lock("shared-resource", backend=sync_backend)


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
