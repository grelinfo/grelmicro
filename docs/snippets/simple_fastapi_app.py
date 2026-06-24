import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.coordination import (
    Coordination,
    LeaderElection,
    Lock,
    TaskLock,
)
from grelmicro.coordination.memory import MemoryLeaderElectionAdapter
from grelmicro.log import configure
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakerRegistry
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.task import Tasks

logger = logging.getLogger(__name__)

# === grelmicro ===
tasks = Tasks()
leader_election = LeaderElection("leader-election")
tasks.add_task(leader_election)

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(
    uses=[
        Coordination(lock=redis.lock(), election=MemoryLeaderElectionAdapter()),
        CircuitBreakerRegistry(MemoryCircuitBreakerAdapter()),
        tasks,
    ]
)


# === FastAPI ===
@asynccontextmanager
async def lifespan(app):
    configure()
    yield


app = FastAPI(lifespan=lifespan)
micro.install(app)


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
@tasks.interval(seconds=60, lock=TaskLock(lease_duration=300))
def cleanup():
    logger.info("cleanup")


# --- Leader-gated Task: only the leader executes ---
@tasks.interval(seconds=10, leader=leader_election)
def leader_only_task():
    logger.info("leader task")
