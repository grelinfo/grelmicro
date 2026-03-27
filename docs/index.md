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

Grelmicro is for any Python application that needs to coordinate work across multiple processes: microservices, modular monoliths, multi-worker deployments, or traditional applications scaling beyond a single process.

- **Easy to use.** Simple decorators and context managers that do the right thing out of the box. No boilerplate, no complex configuration.

- **Async by default.** All I/O operations use `async`/`await`. Async is the natural fit for applications that spend most of their time waiting on network and disk, and it integrates cleanly with FastAPI and FastStream.

- **Backend-agnostic.** Every feature is defined by a protocol, not tied to a specific technology. Swap Redis for PostgreSQL, or use the in-memory backend for tests, without changing application code.

- **Lightweight.** A toolkit, not a framework. Pick the modules you need, ignore the rest. No mandatory configuration, no magic.

The long-term goal is to grow grelmicro into an enterprise-grade toolkit, and eventually rewrite performance-critical components in Rust for better throughput and safety.

## Overview

- **[Cache](https://grelinfo.github.io/grelmicro/cache/)**: In-memory and Redis-backed caching with `@cached` decorator, per-key stampede protection, and swappable backends.
- **[Sync](https://grelinfo.github.io/grelmicro/sync/)**: Distributed locks, leader election, and task locks across Redis, PostgreSQL, SQLite, Kubernetes, and in-memory backends.
- **[Task](https://grelinfo.github.io/grelmicro/task/)**: Periodic task execution with distributed locking for at-most-once semantics across workers.
- **[Resilience](https://grelinfo.github.io/grelmicro/resilience/)**: Circuit breaker for fault tolerance when calling unreliable services.
- **[Logging](https://grelinfo.github.io/grelmicro/logging/)**: 12-factor-compliant logging with JSON/text formatting and timezone support.

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

- `loguru`: A Python logging library.
- `orjson`: A fast, correct JSON library for Python.

### `redis` Dependencies

When you install grelmicro with `pip install grelmicro[redis]` it comes with:

- `redis-py`: The Python interface to the Redis key-value store (the async interface depends on `asyncio`).

### `postgres` Dependencies

When you install grelmicro with `pip install grelmicro[postgres]` it comes with:

- `asyncpg`: The Python `asyncio` interface for PostgreSQL.

## License

This project is licensed under the terms of the MIT license.
