# grelmicro

grelmicro is a lightweight toolkit for building Python applications that need to coordinate work across processes, workers, or services.

[![PyPI - Version](https://img.shields.io/pypi/v/grelmicro)](https://pypi.org/project/grelmicro/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/grelmicro)](https://pypi.org/project/grelmicro/)
[![codecov](https://codecov.io/gh/grelinfo/grelmicro/graph/badge.svg?token=GDFY0AEFWR)](https://codecov.io/gh/grelinfo/grelmicro)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)

______________________________________________________________________

**Documentation**: [https://grelinfo.github.io/grelmicro/](https://grelinfo.github.io/grelmicro)

**Source Code**: [https://github.com/grelinfo/grelmicro](https://github.com/grelinfo/grelmicro)

______________________________________________________________________

## Vision

grelmicro is for any Python application that needs to coordinate work across multiple processes: microservices, modular monoliths, multi-worker deployments, or traditional applications scaling beyond a single process.

- **Easy to use.** Simple decorators and context managers that do the right thing out of the box. No boilerplate, no complex configuration.

- **Async by default.** All I/O operations use `async`/`await`. Async is the natural fit for applications that spend most of their time waiting on network and disk, and it integrates cleanly with FastAPI and FastStream.

- **Backend-agnostic.** Every feature is defined by a protocol, not tied to a specific technology. Swap Redis for PostgreSQL, or use the in-memory backend for tests, without changing application code.

- **Lightweight.** A toolkit, not a framework. Pick the modules you need, ignore the rest. Minimal configuration: just register a backend and start using it.

The long-term goal is to grow grelmicro into an enterprise-grade toolkit, and eventually rewrite performance-critical components in Rust for better throughput and safety.

## Overview

### [Cache](cache.md)

The `cache` module provides a `@cached` decorator with per-key stampede protection. Choose the cache that fits your use case: `TTLCache` for fast in-memory caching within a single process, or `RedisCache` for shared caching across multiple processes.

### [Synchronization Primitives](sync.md)

The `sync` module provides distributed coordination primitives, technology-agnostic across Redis, PostgreSQL, SQLite, Kubernetes, and in-memory backends.

- **Lock**: Distributed lock for synchronizing access to shared resources.
- **Task Lock**: Distributed lock for scheduled tasks with minimum and maximum hold times.
- **Leader Election**: Single-leader election for running tasks only once in a cluster.

### [Task Scheduler](task.md)

The `task` module provides periodic task execution with optional distributed locking for at-most-once semantics across workers. Not a replacement for Celery or APScheduler: it is lightweight, easy to use, and built for coordination.

### [Resilience](resilience.md)

The `resilience` module provides patterns to protect services from failures and overload.

- **Circuit Breaker**: Automatically detects repeated failures and temporarily blocks calls to unstable services.
- **Rate Limiter**: Limits the number of requests per time window using the GCRA algorithm with Redis or in-memory backends.

### [Logging](logging.md)

The `logging` module provides 12-factor-compliant logging with JSON/text formatting, configurable timezone, and environment variable configuration (`LOG_LEVEL`, `LOG_FORMAT`, `LOG_TIMEZONE`).

### [Tracing](tracing.md)

The `tracing` module provides unified instrumentation inspired by Rust's `tracing` crate. The `@instrument` decorator creates OTel spans and enriches log records with structured context automatically.

## Installation

```bash
pip install grelmicro
```

## Example

### FastAPI Integration

Create a file `main.py` with:

```python
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from grelmicro.cache import JsonSerializer, TTLCache, cached
from grelmicro.cache.redis import RedisCacheBackend
from grelmicro.logging import configure_logging
from grelmicro.resilience.circuitbreaker import CircuitBreaker
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.ratelimiter import RateLimiter
from grelmicro.resilience.redis import RedisRateLimiterBackend
from grelmicro.sync import LeaderElection, Lock
from grelmicro.sync.redis import RedisSyncBackend
from grelmicro.task import TaskManager

logger = logging.getLogger(__name__)

# === grelmicro ===
task = TaskManager()
sync_backend = RedisSyncBackend("redis://localhost:6379/0")
cache_backend = RedisCacheBackend("redis://localhost:6379/0", prefix="myapp:")
rate_limit_backend = RedisRateLimiterBackend("redis://localhost:6379/0")
leader_election = LeaderElection("leader-election")
task.add_task(leader_election)

cache = TTLCache(ttl=300, serializer=JsonSerializer())


# === FastAPI ===
@asynccontextmanager
async def lifespan(app):
    configure_logging()
    async with sync_backend, cache_backend, rate_limit_backend, task:
        yield


app = FastAPI(lifespan=lifespan)


# --- Cache: avoid redundant database queries ---
@cached(cache)
async def get_user(user_id: int) -> dict:
    return {"id": user_id, "name": "Alice"}


@app.get("/users/{user_id}")
async def read_user(user_id: int):
    return await get_user(user_id)


# --- Circuit Breaker: protect calls to an unreliable service ---
cb = CircuitBreaker("my-service")


@app.get("/")
async def read_root():
    async with cb:
        return {"Hello": "World"}


# --- Rate Limiter: protect endpoints from overload ---
api_limiter = RateLimiter("api", limit=100, window=60)


@app.get("/api")
async def api_endpoint(request: Request):
    try:
        await api_limiter.acquire_or_raise(key=request.client.host)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(int(exc.retry_after))},
        )
    return {"status": "ok"}


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


# --- Distributed Task: run once per interval across all workers ---
@task.interval(seconds=60, max_lock_seconds=300)
def cleanup():
    logger.info("cleanup")


# --- Leader-gated Task: only the leader executes ---
@task.interval(seconds=10, leader=leader_election)
def leader_only_task():
    logger.info("leader task")
```

## Dependencies

grelmicro depends on Pydantic v2+, AnyIO v4+, and FastDepends.

### `standard` Dependencies

When you install grelmicro with `pip install grelmicro[standard]` it comes with:

- `orjson`: A fast, correct JSON library for Python.

### `redis` Dependencies

When you install grelmicro with `pip install grelmicro[redis]` it comes with:

- `redis-py`: The Python interface to the Redis key-value store (the async interface depends on `asyncio`).

### `postgres` Dependencies

When you install grelmicro with `pip install grelmicro[postgres]` it comes with:

- `asyncpg`: The Python `asyncio` interface for PostgreSQL.

## License

This project is licensed under the terms of the MIT license.
