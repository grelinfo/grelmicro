# Coming from APScheduler, slowapi or aioredlock

APScheduler, slowapi and aioredlock each solve one problem well. Two of them change behavior once the service runs as more than one replica. This page shows what you have today, what changes on several replicas, the grelmicro code that replaces it, and when to keep the library you have.

## APScheduler

This section covers APScheduler 3, the current stable release.

### What you have

A scheduler started in the FastAPI lifespan, with one interval job and one cron job:

```python
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

scheduler = AsyncIOScheduler(timezone="Europe/Zurich")


async def cleanup():
    ...


async def nightly_report():
    ...


scheduler.add_job(cleanup, "interval", minutes=1)
scheduler.add_job(nightly_report, "cron", hour=2)


@asynccontextmanager
async def lifespan(app):
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
```

### On more than one replica

Every replica starts its own scheduler, so three replicas run each job three times. A shared job store does not fix it: the APScheduler 3 docs say a job store must never be shared between schedulers. APScheduler 4, still in alpha, adds data stores that several schedulers share.

### With grelmicro

`gate="claim"` runs each interval and each cron fire on one replica, whichever claims it first:

```python
--8<-- "coming_from/scheduled_jobs.py"
```

The claims live in Redis. PostgreSQL and SQLite work the same way, see [Coordination](../coordination/index.md).

### Mapping

| APScheduler | grelmicro |
|---|---|
| `AsyncIOScheduler()` | `Tasks()` |
| `scheduler.start()`, `scheduler.shutdown()` | `micro.install(app)`, or `async with micro:` |
| `add_job(func, "interval", minutes=1)` | `@tasks.every(interval=60)` |
| `add_job(func, "cron", hour=2)` | `@tasks.cron("0 2 * * *")` |
| `CronTrigger.from_crontab("0 2 * * *")` | `@tasks.cron("0 2 * * *")` |
| `AsyncIOScheduler(timezone="Europe/Zurich")` | `Tasks(timezone="Europe/Zurich")` |
| `max_instances=1` | Always. The next interval starts once the run ends |
| `misfire_grace_time=600` | `@tasks.cron(..., gate="claim", misfire_grace=600)` |
| `coalesce=True` | Always for a claimed cron task: only the most recent missed fire runs |
| One run per replica | `gate="claim"`, or `gate=leader` with a `LeaderElection` |
| `"date"` trigger | None |
| A persistent job store | None. Tasks are declared in code |

An APScheduler interval counts from the start of one run to the start of the next. A grelmicro interval counts from the end of one run to the start of the next. See [Task Scheduler](../task.md).

### When to keep APScheduler

Keep APScheduler for one process, for one-off `date` jobs, or for jobs added at runtime and kept in a persistent job store.

## slowapi

### What you have

A limit of five calls a minute on one route:

```python
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.get("/home")
@limiter.limit("5/minute")
async def homepage(request: Request):
    return PlainTextResponse("test")
```

### On more than one replica

Without `storage_uri`, slowapi counts in memory, so each replica keeps its own count and three replicas allow up to fifteen calls a minute. Behind a proxy, `get_remote_address` reads the proxy's address, so every caller shares one budget.

### With grelmicro

`RateLimited` meters the route, counts in Redis, and reads the caller from the proxies you trust:

```python
--8<-- "coming_from/route_rate_limit.py"
```

The sixth call in a minute gets a `429` with `Retry-After`, `RateLimit` and `RateLimit-Policy` headers. See [Rate limit](../http/rate-limit.md).

### Mapping

| slowapi | grelmicro |
|---|---|
| `@limiter.limit("5/minute")` | `dependencies=[RateLimited(RateLimiter.sliding_window("home", limit=5, window=60))]` |
| `Limiter(default_limits=["100/minute"])` | `RateLimitedRequests(limiter, trusted=...)` for every route |
| `Limiter(storage_uri="redis://...")` | `RedisProvider(...)` in `Grelmicro(uses=[...])` |
| `key_func=get_remote_address` | `trusted=TrustedProxies([...])` |
| A custom `key_func` | `key=` |
| `strategy="fixed-window"` | `RateLimiter.sliding_window(...)` or `RateLimiter.token_bucket(...)` |
| `app.add_exception_handler(RateLimitExceeded, ...)` | `ErrorResponses()` answers `429` as a problem detail |
| `headers_enabled=True` | Always on. `legacy_headers=True` adds `X-RateLimit-*` |
| The `request: Request` parameter | Not needed |

### When to keep slowapi

Keep slowapi for one process, or when the counts must live in Memcached or MongoDB, which grelmicro does not support.

## aioredlock

### What you have

A lock around one order, held for at most ten seconds:

```python
from aioredlock import Aioredlock, LockError

lock_manager = Aioredlock(["redis://localhost:6379/0"])


async def charge(order_id: str) -> None:
    try:
        async with await lock_manager.lock(f"order:{order_id}", lock_timeout=10):
            ...
    except LockError:
        ...
```

### On more than one replica

aioredlock already works across replicas. It locks on Redis only, and its last release, 0.7.3, dates from February 2022.

### With grelmicro

`Lock` holds the same lease on Redis, PostgreSQL, SQLite or Kubernetes:

```python
--8<-- "coming_from/shared_lock.py"
```

Every hold carries a fencing token, so the resource you write to can refuse a holder whose lease ran out. See [Lock](../coordination/lock.md).

### Mapping

| aioredlock | grelmicro |
|---|---|
| `Aioredlock([...])` | `RedisProvider(...)` in `Grelmicro(uses=[...])` |
| `lock_manager.lock("r", lock_timeout=10)` | `Lock("r", lease_duration=10)` |
| `async with await lock_manager.lock("r"):` | `async with Lock("r"):` |
| `retry_count`, `retry_delay_min`, `retry_delay_max` | `hold(timeout=5)` bounds the wait, `retry_interval` and `retry_jitter` pace it |
| `LockError` | `LockTimeoutError` from `hold(timeout=...)` or `acquire(timeout=...)` |
| `await lock_manager.extend(lock)` | `await lock.extend()` |
| `await lock_manager.unlock(lock)` | `await lock.release()` |
| `await lock_manager.is_locked("r")` | `await lock.locked()` |
| `await lock_manager.destroy()` | Closed with the app, by `async with micro:` or `micro.install(app)` |
| None | `held.fencing_token` |

### When to keep aioredlock

Keep aioredlock when you want the Redlock algorithm over several independent Redis servers. A grelmicro lock uses one.

## Others

- **pybreaker**: a breaker keeps its state in one process by default. A grelmicro `CircuitBreaker` keeps one state for every replica in Redis or PostgreSQL. See [Circuit Breaker](../resilience/circuit-breaker.md).
- More libraries side by side, tenacity and aiocache among them, are on the [Comparison](../comparison.md) page.
