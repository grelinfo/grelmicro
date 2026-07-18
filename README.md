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
  <a href="https://securityscorecards.dev/viewer/?uri=github.com/grelinfo/grelmicro"><img alt="OpenSSF Scorecard" src="https://api.securityscorecards.dev/projects/github.com/grelinfo/grelmicro/badge"></a>
</p>

<p align="center">
  <img alt="A FastAPI route protected by a grelmicro rate limiter and health check" src="docs/img/demo.gif" width="800">
</p>

______________________________________________________________________

**Documentation**: [https://grelinfo.github.io/grelmicro/](https://grelinfo.github.io/grelmicro)

**Source Code**: [https://github.com/grelinfo/grelmicro](https://github.com/grelinfo/grelmicro)

______________________________________________________________________

## Why grelmicro

grelmicro ships microservice patterns as small, composable modules with pluggable backends: locks, rate limits, circuit breakers, cache, the transactional outbox, logging, health checks, and task scheduling. Async-first, type-safe, and fully tested.

It is built for any Python application that coordinates work across processes, workers, or replicas. The same primitives serve microservices, a modular monolith, or a self-contained system, and fit naturally into containerized and Kubernetes deployments.

- **Micro**: one focused primitive per module. Each covers a microservice pattern (distributed lock, leader election, rate limiter, circuit breaker, health check API, externalised configuration).
- **Fast**: small footprint by design. We keep the layers thin so your code stays quick.
- **Async-first**: every I/O call is `async` / `await`. Drops into FastAPI, FastStream, and any asyncio-based stack.
- **Backend-agnostic**: each primitive is a protocol. Swap Redis for PostgreSQL or SQLite without touching application code.
- **Railguarded**: fully tested, type-checked, and validated. Pre-1.0 the API may change on a minor release. `1.x` follows standard semver.

grelmicro is **not** a task queue (reach for Celery, Dramatiq, or taskiq) and **not** a web framework (it plugs into FastAPI, Starlette, or Litestar). It fills the gap between the web framework you picked and the infrastructure you run.

Already using `aiocache`, `slowapi`, `pybreaker`, `tenacity`, or `aioredlock`? See the [comparison page](https://grelinfo.github.io/grelmicro/comparison/) for a per-domain breakdown.

## Modules

| Module | Summary |
|---|---|
| [**Cache**](https://grelinfo.github.io/grelmicro/cache/) | `@cached` decorator with local and distributed stampede protection. In-memory `TTLCache` or `RedisCacheAdapter`. |
| [**Idempotency**](https://grelinfo.github.io/grelmicro/idempotency/) | Idempotency keys that make a retried operation safe. Store the response once, replay it on repeat, single-flight across replicas. |
| [**Coordination**](https://grelinfo.github.io/grelmicro/coordination/) | Distributed `Lock`, `TaskLock`, and `LeaderElection`. Redis, PostgreSQL, SQLite, Kubernetes, in-memory. |
| [**Outbox**](https://grelinfo.github.io/grelmicro/outbox/) | Transactional outbox. `publish` a message inside your database transaction and a background relay delivers it at least once with retries and dead-lettering. PostgreSQL, in-memory. |
| [**Task Scheduler**](https://grelinfo.github.io/grelmicro/task/) | Interval and cron tasks with durable, distributed at-most-once execution. A modern, lightweight alternative to APScheduler and Celery beat. |
| [**Resilience**](https://grelinfo.github.io/grelmicro/resilience/) | [Circuit Breaker](https://grelinfo.github.io/grelmicro/resilience/circuit-breaker/) and [Rate Limiter](https://grelinfo.github.io/grelmicro/resilience/rate-limiter/) with pluggable algorithms (`TokenBucketConfig`, `SlidingWindowConfig`). |
| [**Logging**](https://grelinfo.github.io/grelmicro/logging/) | 12-factor logging with JSON, LOGFMT, TEXT, or PRETTY output, structured error rendering, and OpenTelemetry trace context. |
| [**Tracing**](https://grelinfo.github.io/grelmicro/tracing/) | Unified instrumentation. `@instrument` creates OpenTelemetry spans and enriches log records with structured context. |
| [**Metrics**](https://grelinfo.github.io/grelmicro/metrics/) | OpenTelemetry metrics with a `@measure` decorator, a Prometheus `/metrics` router, and built-in instrumentation across components. |
| [**Health**](https://grelinfo.github.io/grelmicro/health/) | Health check registry with concurrent runners and FastAPI liveness / readiness integration. |
| [**Configuration**](https://grelinfo.github.io/grelmicro/config/) | `ExternalConfig` reconfigures live components from a mounted ConfigMap, Secret, or `.env` / JSON / YAML / TOML file. |

## Installation

```bash
pip install grelmicro
```

See the [Installation guide](https://grelinfo.github.io/grelmicro/installation/) for `uv` and `poetry` commands, plus optional extras for Redis, PostgreSQL, SQLite, Kubernetes, OpenTelemetry, and structlog.

## Example

### Run the demo

Want to see every Pattern running against real Redis and Postgres? The [FastAPI demo](https://github.com/grelinfo/grelmicro/tree/main/examples/fastapi-demo) starts in three commands:

```bash
cd examples/fastapi-demo
docker compose up --wait
open http://localhost:8000/docs
```

It wires a cached endpoint, a rate-limited endpoint, a circuit-breaker-protected endpoint, a distributed lock, a leader-gated task, and `/healthz` / `/readyz` probes. Read [`app.py`](https://github.com/grelinfo/grelmicro/blob/main/examples/fastapi-demo/app.py) to see each one.

### One route, one primitive

The smallest grelmicro program: a FastAPI route protected by a process-local rate limiter. No `Grelmicro(...)`, no Redis, no lifespan.

```python
from fastapi import FastAPI

from grelmicro.resilience import (
    MemoryRateLimiterAdapter,
    RateLimitExceededError,
    RateLimiter,
)

app = FastAPI()
api_limiter = RateLimiter.sliding_window(
    "api", limit=100, window=60, backend=MemoryRateLimiterAdapter()
)


@app.get("/ping")
async def ping() -> dict[str, str]:
    try:
        await api_limiter.acquire_or_raise()
    except RateLimitExceededError:
        return {"status": "throttled"}
    return {"status": "ok"}
```

That is the whole thing. Pick a primitive, name it, give it a backend, call it. The memory adapter says per-process on purpose. Swap to a fleet-wide backend later by composing it inside `Grelmicro(uses=[RateLimiterRegistry(redis)])` as shown below.

### FastAPI with one provider and one component

To make the rate limiter fleet-wide, wrap it in a `Grelmicro` container with one provider and one component, then install it into FastAPI.

```python
from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import (
    RateLimitExceededError,
    RateLimiter,
    RateLimiterRegistry,
)

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[RateLimiterRegistry(redis)])

api_limiter = RateLimiter.sliding_window("api", limit=100, window=60)

app = FastAPI()
micro.install(app)


@app.get("/ping")
async def ping() -> dict[str, str]:
    try:
        await api_limiter.acquire_or_raise()
    except RateLimitExceededError:
        return {"status": "throttled"}
    return {"status": "ok"}
```

Adding more primitives is the same shape: one extra entry in `uses=[...]`. `micro.install(app)` opens the app on startup, closes it on shutdown, and lets request handlers resolve backends without passing `backend=`.

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
    CircuitBreakerRegistry,
    RateLimitExceededError,
    RateLimiter,
    RateLimiterRegistry,
)
from grelmicro.resilience.circuitbreaker.memory import MemoryCircuitBreakerAdapter
from grelmicro.coordination import Coordination, LeaderElection, Lock, TaskLock
from grelmicro.task import Tasks

logger = logging.getLogger(__name__)

# === grelmicro app: one container, one lifespan ===
tasks = Tasks()
health = HealthChecks()

redis = RedisProvider("redis://localhost:6379/0")

leader = LeaderElection("leader-election")
tasks.add_task(leader)

micro = Grelmicro(uses=[
    Coordination(redis),
    Cache(redis),
    RateLimiterRegistry(redis),
    CircuitBreakerRegistry(MemoryCircuitBreakerAdapter()),
    tasks,
    health,
])

# === Patterns declared once at module load, no backend wiring ===
ttl_cache = TTLCache(ttl=300, serializer=JsonSerializer())
lock = Lock("shared-resource")
cb = CircuitBreaker("my-service")
api_limiter = RateLimiter.sliding_window("api", limit=100, window=60)


# === FastAPI ===
@asynccontextmanager
async def lifespan(app):
    configure_logging()
    yield


app = FastAPI(lifespan=lifespan)
micro.install(app)


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
@tasks.every(seconds=5)
def heartbeat():
    logger.info("heartbeat")


# --- Distributed Task: run once per interval across all workers ---
@tasks.every(seconds=60, lock=TaskLock(lease_duration=300))
def cleanup():
    logger.info("cleanup")


# --- Leader-gated Task: only the leader executes ---
@tasks.every(seconds=10, leader=leader)
def leader_only_task():
    logger.info("leader task")
```

The key shape:

- **One container, one lifespan.** `Grelmicro(uses=[...])` lists every Component and active manager. `async with micro:` opens them all in order, closes in reverse.
- **One Provider, many Components.** `Coordination(redis)`, `Cache(redis)`, `RateLimiterRegistry(redis)` all share the same `RedisProvider` pool. List the Components and grelmicro lifecycles the Provider once. Pass a bare `Grelmicro(uses=[redis])` to register a default Component per kind the Provider serves.
- **Patterns are declared at module load.** `Lock("cart")`, `TTLCache(ttl=60)`, `CircuitBreaker("svc")` carry no backend reference. They resolve through the active app inside `async with`, and `GrelmicroMiddleware` extends that scope to request handlers. The same `Lock` works in production with Redis and in tests with `MemoryLockAdapter`, no rewiring.
- **Pay only for what you import.** `import grelmicro` does not pull in `redis`, `psycopg`, or any other vendor SDK. First-party Providers live under `grelmicro.providers.{vendor}` and load only when you import them.

For multiple Redis instances, separate names, or test overrides, see the [docs](https://grelinfo.github.io/grelmicro/).

## License

This project is licensed under the terms of the [MIT license](https://github.com/grelinfo/grelmicro/blob/main/LICENSE).
