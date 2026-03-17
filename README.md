# grelmicro

grelmicro is a lightweight framework/toolkit which is ideal for building async microservices in Python.

It is the perfect companion for building cloud-native applications with FastAPI and FastStream, providing essential tools for running in distributed and containerized environments.

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

## Overview

grelmicro provides essential features for building robust distributed systems, including:

- **Backends**: Technology-agnostic design supporting Redis, PostgreSQL, and in-memory backends for testing.
- **Logging**: Easy-to-configure logging with support for both text or JSON structured format with configurable timezone.
- **Resilience Patterns**: Implements common resilience patterns like retries and circuit breakers.
- **Synchronization Primitives**: Includes leader election and distributed lock mechanisms.
- **Task Scheduler**: A simple and efficient task scheduler for running periodic tasks.

These features address common challenges in microservices and distributed, containerized systems while maintaining ease of use.

### [Logging](https://grelinfo.github.io/grelmicro/logging/)

The `logging` package provides a simple and easy-to-configure logging system.

The logging feature adheres to the 12-factor app methodology, directing logs to `stdout`. It supports JSON and TEXT formatting with configurable timezone support and allows log level configuration via environment variables (`LOG_LEVEL`, `LOG_FORMAT`, `LOG_TIMEZONE`).

### [Resilience Patterns](https://grelinfo.github.io/grelmicro/resilience/)

The `resilience` package provides higher-order functions (decorators) that implement resilience patterns to improve fault tolerance and reliability in distributed systems.


- **Circuit Breaker**: Automatically detects repeated failures and temporarily blocks calls to unstable services, allowing them time to recover.

### [Synchronization Primitives](https://grelinfo.github.io/grelmicro/sync/)

The `sync` package provides synchronization primitives for distributed systems.

The primitives are technology agnostic, supporting multiple backends like Redis, PostgreSQL, and in-memory for testing.

The available primitives are:

- **Task Lock**: A distributed lock for scheduled tasks with minimum and maximum hold times. Best used via the [`interval()` decorator with `max_lock_seconds`](https://grelinfo.github.io/grelmicro/task/#distributed-lock) which configures it automatically.
- **Leader Election**: A single worker is elected as the leader for performing tasks only once in a cluster.
- **Lock**: A distributed lock that can be used to synchronize access to shared resources.

### [Task Scheduler](https://grelinfo.github.io/grelmicro/task/)

The `task` package provides a simple task scheduler that can be used to run tasks periodically.

> **Note**: This is not a replacement for bigger tools like Celery, taskiq, or APScheduler. It is just lightweight, easy to use, and safe for running tasks in a distributed system with synchronization.

The key features are:

- **Fast & Easy**: Offers simple decorators to define and schedule tasks effortlessly.
- **Interval Task**: Allows tasks to run at specified intervals.
- **Synchronization**: Controls concurrency using synchronization primitives to manage simultaneous task execution (see the `sync` package).
- **Dependency Injection**: Use [FastDepends](https://lancetnik.github.io/FastDepends/) library to inject dependencies into tasks.
- **Error Handling**: Catches and logs errors, ensuring that task execution errors do not stop the scheduling.

## Installation

```bash
pip install grelmicro
```

## Examples

### FastAPI Integration

- Create a file `main.py` with:

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
