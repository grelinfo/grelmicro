# Grelmicro

Grelmicro is a lightweight framework/toolkit which is ideal for building async microservices in Python.

It is the perfect companion for building cloud-native applications with FastAPI and FastStream, providing essential tools for running in distributed and containerized environments.

[![PyPI - Version](https://img.shields.io/pypi/v/grelmicro)](https://pypi.org/project/grelmicro/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/grelmicro)](https://pypi.org/project/grelmicro/)
[![codecov](https://codecov.io/gh/grelinfo/grelmicro/graph/badge.svg?token=GDFY0AEFWR)](https://codecov.io/gh/grelinfo/grelmicro)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)

______________________________________________________________________

**Documentation**: [https://grelmicro.grel.info](https://grelmicro.grel.info)

**Source Code**: [https://github.com/grelinfo/grelmicro](https://github.com/grelinfo/grelmicro)

______________________________________________________________________

## Overview

Grelmicro provides essential features for building robust distributed systems, including:

- **Backends**: Technology-agnostic design supporting Redis, PostgreSQL, and in-memory backends for testing.
- **Logging**: Easy-to-configure logging with support of both text or JSON structured format.
- **Task Scheduler**: A simple and efficient task scheduler for running periodic tasks.
- **Synchronization Primitives**: Includes leader election and distributed lock mechanisms.

These features address common challenges in microservices and distributed, containerized systems while maintaining ease of use.

### Logging

The `logging` package provides a simple and easy-to-configure logging system.

The logging feature adheres to the 12-factor app methodology, directing logs to `stdout`. It supports JSON formatting and allows log level configuration via environment variables.

### Synchronization Primitives

The `sync` package provides synchronization primitives for distributed systems.

The primitives are technology agnostic, supporting multiple backends like Redis, PostgreSQL, and in-memory for testing.

The available primitives are:

- **Leader Election**: A single worker is elected as the leader for performing tasks only once in a cluster.
- **Lock**: A distributed lock that can be used to synchronize access to shared resources.

### Task Scheduler

The `task` package provides a simple task scheduler that can be used to run tasks periodically.

> **Note**: This is not a replacement for bigger tools like Celery, taskiq, or APScheduler. It is just lightweight, easy to use, and safe for running tasks in a distributed system with synchronization.

The key features are:

- **Fast & Easy**: Offers simple decorators to define and schedule tasks effortlessly.
- **Interval Task**: Allows tasks to run at specified intervals.
- **Synchronization**: Controls concurrency using synchronization primitives to manage simultaneous task execution (see the `sync` package).
- **Dependency Injection**: Use [FastDepends](https://lancetnik.github.io/FastDepends/) library to inject dependencies into tasks.
- **Error Handling**: Catches and logs errors, ensuring that task execution failures do not stop the scheduling.

## Installation

```bash
pip install grelmicro
```

## Examples

### FastAPI Integration

- Create a file `main.py` with:

```python
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
```

## Dependencies

Grelmicro depends on Pydantic v2+, AnyIO v4+, and FastDepends.

### `standard` Dependencies

When you install Grelmicro with `pip install grelmicro[standard]` it comes with:

- `loguru`: A Python logging library.
- `orjson`: A fast, correct JSON library for Python.

### `redis` Dependencies

When you install Grelmicro with `pip install grelmicro[redis]` it comes with:

- `redis-py`: The Python interface to the Redis key-value store (the async interface depends on `asyncio`).

### `postgres` Dependencies

When you install Grelmicro with `pip install grelmicro[postgres]` it comes with:

- `asyncpg`: The Python `asyncio` interface for PostgreSQL.

## License

This project is licensed under the terms of the MIT license.
