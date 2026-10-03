<p align="center">
  <a href="https://grelmicro.grel.info">
    <img alt="grelmicro" class="grel-wordmark" src="img/logo/wordmark.svg" width="360">
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
  <a href="https://www.bestpractices.dev/projects/13655"><img alt="OpenSSF Best Practices" src="https://www.bestpractices.dev/projects/13655/badge"></a>
  <a href="https://slsa.dev/spec/latest/levels"><img alt="SLSA Build Level 2" src="https://slsa.dev/images/gh-badge-level2.svg"></a>
</p>

<p align="center">
  <img alt="A FastAPI route protected by a grelmicro rate limiter and health check" src="img/demo.gif" width="800">
</p>

______________________________________________________________________

**Documentation**: [https://grelmicro.grel.info/](https://grelmicro.grel.info)

**Source Code**: [https://github.com/grelinfo/grelmicro](https://github.com/grelinfo/grelmicro)

______________________________________________________________________

## What your service gains

Your FastAPI, Starlette, Litestar or FastStream service keeps its routes, its lifespan and its database code. grelmicro adds what a service needs once it runs on more than one replica:

- a lock, a cache and a rate limit that every replica shares,
- a scheduled job that runs on one replica, not on all of them,
- a retry, a circuit breaker and a timeout around the calls that fail,
- health probes, logs, traces and metrics that Kubernetes and your dashboards read.

One line says where the shared state lives: Redis, Valkey, PostgreSQL, SQLite or Kubernetes. Change that line and the rest of the code stays the same.

Every pattern behaves the same on each framework, and a [parity test](https://grelmicro.grel.info/frameworks/#how-the-claim-is-held) holds the claim. The hot paths run in a compiled Rust core: a JWT signature check is about four times faster than in a pure-Python library.

Coming from another stack? See the mapping for [Spring Boot](https://grelmicro.grel.info/coming-from/spring-boot/), [Django](https://grelmicro.grel.info/coming-from/django/) or [Symfony](https://grelmicro.grel.info/coming-from/symfony/).

## Add it to the app you already run

Create a file `main.py` with:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.cache import TTLCache, cached
from grelmicro.coordination import Lock
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import Tasks


@asynccontextmanager
async def lifespan(app):
    print("your own startup")
    yield
    print("your own shutdown")


app = FastAPI(lifespan=lifespan)

tasks = Tasks()
micro = Grelmicro(uses=[RedisProvider("redis://localhost:6379/0"), tasks])
micro.install(app)

prices = TTLCache[int](ttl=30)
checkout_lock = Lock("checkout")


@cached(prices)
async def load_price(sku: str) -> int:
    return 42  # your database call


@app.post("/checkout/{sku}")
async def checkout(sku: str) -> dict[str, int]:
    async with checkout_lock:
        return {"price": await load_price(sku)}


@tasks.every(seconds=60, gate="claim")
async def expire_carts() -> None:
    print("runs on one replica each minute")
```

Run it:

```bash
docker run -d -p 6379:6379 redis
pip install "grelmicro[fastapi,redis]" "fastapi[standard]"
fastapi run main.py
```

Your lifespan still runs. `micro.install(app)` opens grelmicro around it at startup and closes it at shutdown. Start a second copy with `fastapi run main.py --port 8001`: both copies share the lock and the cache, and only one of them runs `expire_carts` each minute.

## What each module guarantees

| Module | Guarantee |
|---|---|
| [**Cache**](https://grelmicro.grel.info/cache/) | `@cached(ttl=30)` memoizes in one process. `@cached(TTLCache(...))` shares entries across replicas, and concurrent misses on one key run the function once per process. `lock=True` runs it once across replicas. |
| [**Idempotency**](https://grelmicro.grel.info/idempotency/) | A repeated key within `ttl` replays the stored response without running the operation again. |
| [**Coordination**](https://grelmicro.grel.info/coordination/) | A `Lock` has one holder at a time for as long as its lease, and a `LeaderElection` one leader. A `lost` renewal metric tells you when the work outran the lease. |
| [**Outbox**](https://grelmicro.grel.info/outbox/) | A message published inside your transaction runs its handler at least once, and never for a transaction that rolled back. |
| [**Task Scheduler**](https://grelmicro.grel.info/task/) | With `gate="claim"`, one worker runs each interval or cron fire. A claimed cron fire runs at most once, and a fire missed while every worker was down replays once. |
| [**Resilience**](https://grelmicro.grel.info/resilience/) | An open [circuit breaker](https://grelmicro.grel.info/resilience/circuit-breaker/) fails calls fast until `reset_timeout`, then lets a few probe calls through. A [Rate Limiter](https://grelmicro.grel.info/resilience/rate-limiter/) on a shared backend counts one budget for every replica. [Retry](https://grelmicro.grel.info/resilience/retry/), [Timeout](https://grelmicro.grel.info/resilience/timeout/), [Bulkhead](https://grelmicro.grel.info/resilience/bulkhead/) and [Fallback](https://grelmicro.grel.info/resilience/fallback/) compose through a [Shield](https://grelmicro.grel.info/resilience/shield/). |
| [**Health**](https://grelmicro.grel.info/health/) | Each check runs at most once per `cache_ttl`, however many probes arrive. |
| [**Security**](https://grelmicro.grel.info/security/) | A [JWT](https://grelmicro.grel.info/security/jwt/) ![Rust powered](https://img.shields.io/badge/Rust-powered-b7410e?logo=rust&logoColor=white) is accepted only with a valid signature and `exp`, and with `aud` and `iss` when you name them. Signing keys refresh from the JWKS when they rotate. [Client IP](https://grelmicro.grel.info/security/clientip/) trusts only your own proxies. |
| [**Logging**](https://grelmicro.grel.info/logging/), [**Tracing**](https://grelmicro.grel.info/tracing/), [**Metrics**](https://grelmicro.grel.info/metrics/) | A log line written inside a traced request carries that request's trace and span id. |
| [**Configuration**](https://grelmicro.grel.info/config/) | `ExternalConfig` changes a running component's settings from a mounted ConfigMap, Secret or file, without a restart. |

grelmicro is **not** a task queue (reach for Celery, Dramatiq, or taskiq), **not** a message broker client (reach for FastStream to publish and subscribe over Kafka, RabbitMQ, NATS, or Redis), and **not** a web framework (it plugs into FastAPI, Starlette, Litestar, and FastStream). It fills the gap between the web framework you picked and the infrastructure you run.

Already using `aiocache`, `slowapi`, `pybreaker`, `tenacity`, or `aioredlock`? See the [comparison page](https://grelmicro.grel.info/comparison/) for a per-domain breakdown.

Pre-1.0, the API may change on a minor release. `1.x` follows standard semver.

## Installation

```bash
pip install grelmicro
```

See the [Installation guide](https://grelmicro.grel.info/installation/) for `uv` and `poetry` commands, plus optional extras for Redis, PostgreSQL, SQLite, Kubernetes, OpenTelemetry, structlog, and JWT verification.

## More examples

### Run the demo

Want to see every pattern running against real Redis and Postgres? The [FastAPI demo](https://github.com/grelinfo/grelmicro/tree/main/examples/fastapi-demo) starts in three commands:

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

from grelmicro.providers.memory import MemoryProvider
from grelmicro.resilience import RateLimitExceededError, RateLimiter

app = FastAPI()
api_limiter = RateLimiter.sliding_window(
    "api", limit=100, window=60, backend=MemoryProvider().ratelimiter()
)


@app.get("/ping")
async def ping() -> str:
    try:
        await api_limiter.acquire_or_raise()
    except RateLimitExceededError:
        return "throttled"
    return "ok"
```

Run it with `fastapi run main.py`, then open `http://localhost:8000/ping`. The memory backend keeps the count in one process on purpose. Add a provider as in the example above to share it across replicas.

The [User Guide](https://grelmicro.grel.info/first-steps/) covers multiple Redis instances, separate names and test overrides.

## Contributing

Report bugs and request features in [GitHub issues](https://github.com/grelinfo/grelmicro/issues/new/choose). The [reporting guide](https://github.com/grelinfo/grelmicro/blob/main/CONTRIBUTING.md#reporting-a-bug) lists what to include and what happens after you file.

To contribute code or docs, read the [contributing guide](https://github.com/grelinfo/grelmicro/blob/main/CONTRIBUTING.md). It explains the pull request process and the requirements for acceptable contributions: the development setup, the code style, and the pre-merge checklist.

Report security issues privately through the [security policy](https://github.com/grelinfo/grelmicro/security/policy), not a public issue.

## License

This project is licensed under the terms of the [MIT license](https://github.com/grelinfo/grelmicro/blob/main/LICENSE).
