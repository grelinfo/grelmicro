<p align="center">
  <a href="https://grelinfo.github.io/grelmicro">
    <img alt="grelmicro" class="grel-wordmark" src="docs/img/logo/wordmark.svg" width="360">
  </a>
</p>

<p align="center">
  <em>Async-first toolkit. Microservice patterns inside.</em>
</p>

<p align="center">
  A Python toolkit for distributed systems: microservices, modular monoliths, and self-contained systems.
</p>

<p align="center">
  <a href="https://pypi.org/project/grelmicro/"><img alt="PyPI - Version" src="https://img.shields.io/pypi/v/grelmicro"></a>
  <a href="https://pypi.org/project/grelmicro/"><img alt="PyPI - Python Version" src="https://img.shields.io/pypi/pyversions/grelmicro"></a>
  <a href="https://github.com/grelinfo/grelmicro/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-green.svg"></a>
  <a href="https://codecov.io/gh/grelinfo/grelmicro"><img alt="codecov" src="https://codecov.io/gh/grelinfo/grelmicro/graph/badge.svg?token=GDFY0AEFWR"></a>
  <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json"></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="https://github.com/astral-sh/ty"><img alt="ty" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json"></a>
</p>

> **Project status: Active development.** grelmicro is pre-1.0. The public API is not yet stable. Breaking changes are allowed on `MINOR` bumps (`0.14.0` → `0.15.0`) and never on `PATCH`. Pin the minor: `grelmicro>=0.14.0,<0.15.0`. After `1.0.0`, standard semver applies. See the [versioning policy](https://github.com/grelinfo/grelmicro/blob/main/CONTRIBUTING.md#about-grelmicro-versions).

______________________________________________________________________

**Documentation**: [https://grelinfo.github.io/grelmicro/](https://grelinfo.github.io/grelmicro)

**Source Code**: [https://github.com/grelinfo/grelmicro](https://github.com/grelinfo/grelmicro)

______________________________________________________________________

## Why grelmicro

Stop reinventing the wheel. grelmicro ships microservice patterns as small, composable modules with pluggable backends: locks, rate limits, circuit breakers, cache, logging, health checks, and task scheduling. Async-first, type-safe, and battle-tested in production.

It is built for any Python application that coordinates work across processes, workers, or replicas. The same primitives serve every **distributed system**, whether you call it **microservices**, a **modular monolith**, or a **self-contained system**. A distributed lock is a distributed lock whether your system is one process or fifty. It fits naturally into **cloud-native applications**, **containerized apps**, and **Kubernetes** deployments.

- **Micro**: one focused primitive per module, each a canonical microservice pattern (distributed lock, leader election, rate limiter, circuit breaker, health check API, externalised configuration).
- **Fast**: small footprint by design. We keep the layers thin so your code stays quick.
- **Async-first**: every I/O call is `async` / `await`. Drops into FastAPI, FastStream, and any asyncio-based stack.
- **Backend-agnostic**: each primitive is a protocol. Swap Redis for PostgreSQL or SQLite without touching application code.
- **Railguarded**: 100% pytest coverage, ty-checked, ruff-linted, Pydantic-validated. Pre-1.0 API may shift on minor bumps. `1.x` commits to standard semver.

Already using `aiocache`, `slowapi`, `pybreaker`, `tenacity`, or `aioredlock`? See the [comparison page](docs/comparison.md) for a per-domain breakdown.

## Modules

| Module | Summary |
|---|---|
| [**Cache**](docs/cache.md) | `@cached` decorator with per-key stampede protection. In-memory `TTLCache` or `RedisCacheAdapter`. |
| [**Synchronization**](docs/sync.md) | Distributed `Lock`, `TaskLock`, `LeaderElection`. Redis, PostgreSQL, SQLite, Kubernetes, in-memory. |
| [**Task Scheduler**](docs/task.md) | Periodic task execution with optional distributed locking. Lightweight, not a Celery replacement. |
| [**Resilience**](docs/resilience/index.md) | [Circuit Breaker](docs/resilience/circuit-breaker.md) and [Rate Limiter](docs/resilience/rate-limiter.md) with pluggable algorithms (`TokenBucketConfig`, `GCRAConfig`). |
| [**Logging**](docs/logging.md) | 12-factor logging with JSON, LOGFMT, TEXT, or PRETTY output, structured error rendering, and OpenTelemetry trace context. |
| [**Tracing**](docs/tracing.md) | Unified instrumentation. `@instrument` creates OpenTelemetry spans and enriches log records with structured context. |
| [**Health**](docs/health.md) | Health check registry with concurrent runners and FastAPI liveness / readiness integration. |
| [**JSON**](docs/json.md) | Fast JSON via `orjson` when available, with automatic fallback to stdlib `json`. |

## Installation

```bash
pip install grelmicro
```

See the [Installation guide](https://grelinfo.github.io/grelmicro/installation/) for `uv` and `poetry` commands, plus optional extras for Redis, PostgreSQL, SQLite, Kubernetes, OpenTelemetry, and structlog.

## Example

### FastAPI integration

Create a file `main.py` with:

```python
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer, TTLCache, cached
from grelmicro.health import HealthChecks
from grelmicro.log import configure as configure_logging
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import (
    CircuitBreaker,
    RateLimit,
    RateLimitExceededError,
    RateLimiter,
)
from grelmicro.sync import LeaderElection, Lock, Sync
from grelmicro.task import Tasks

logger = logging.getLogger(__name__)

# === grelmicro app: one container, one lifespan ===
tasks = Tasks()
health = HealthChecks()
leader = LeaderElection("leader-election")
tasks.add_task(leader)

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(uses=[
    redis,
    Sync(redis),
    Cache(redis),
    RateLimit(redis),
    tasks,
    health,
])

# === Patterns declared once at module load, resolved at use time ===
ttl_cache = TTLCache(ttl=300, serializer=JsonSerializer())
lock = Lock("shared-resource")
cb = CircuitBreaker("my-service")
api_limiter = RateLimiter.gcra("api", limit=100, window=60)


# === FastAPI lifespan ===
@asynccontextmanager
async def lifespan(app):
    configure_logging()
    async with micro:
        yield


app = FastAPI(lifespan=lifespan)


# --- Cache: avoid redundant database queries ---
@cached(ttl_cache)
async def get_user(user_id: int) -> dict:
    return {"id": user_id, "name": "Alice"}


@app.get("/users/{user_id}")
async def read_user(user_id: int):
    return await get_user(user_id)


# --- Circuit Breaker: protect calls to an unreliable service ---
@app.get("/")
async def read_root():
    async with cb:
        return {"Hello": "World"}


# --- Rate Limiter: protect endpoints from overload ---
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
@tasks.interval(seconds=10, leader=leader)
def leader_only_task():
    logger.info("leader task")
```

The key shape:

- **One container, one lifespan.** `Grelmicro(uses=[...])` lists every Provider, Component, and active manager. `async with micro:` opens them all in order, closes in reverse.
- **One Provider, many Components.** `Sync(redis)`, `Cache(redis)`, `RateLimit(redis)` all share the same `RedisProvider` pool. The Provider holds the connection, the Components attach to it.
- **Patterns are declared at module load.** `Lock("cart")`, `TTLCache(ttl=60)`, `CircuitBreaker("svc")` carry no backend reference. They resolve through the active app inside `async with`. The same `Lock` works in production with Redis and in tests with `MemorySyncAdapter`, no rewiring.
- **Pay only for what you import.** `import grelmicro` does not pull in `redis`, `psycopg`, or any other vendor SDK. First-party Providers live under `grelmicro.providers.{vendor}` and load only when you import them.

For multiple Redis instances, separate names, or test overrides, see the [docs](https://grelinfo.github.io/grelmicro/).

## License

This project is licensed under the terms of the [MIT license](https://github.com/grelinfo/grelmicro/blob/main/LICENSE).
