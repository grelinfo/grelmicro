# Comparison

How grelmicro compares to the focused libraries that cover one Pattern each.

Most Python libraries in this space are **point solutions**: one library for caching, another for rate limiting, another for distributed locks, another for retries, another for circuit breaking, another for health checks. Each ships its own config story, its own lifecycle, its own backend client, its own logging shape.

**grelmicro's unique value is the opposite**: one toolkit that ships every microservice resilience and infrastructure Pattern behind a unified API. One config contract. One lifecycle (`async with micro:`). One Redis client shared by Cache, Lock, RateLimiter, CircuitBreaker. One Postgres client shared by Lock, LeaderElection, TaskLock. One way to reconfigure at runtime. One way to declare health checks.

If you only need a single Pattern, a focused library is often the right pick and this page will say so. If you need two or more, the toolkit story wins by removing four or five config dialects from your codebase.

## What should I pick?

A short decision tree before the detailed tables:

- **I need exactly one primitive** (one cache, one rate limiter, one breaker): pick the focused library for that primitive. The [Quick comparison](#quick-comparison) below names the right one for each Pattern.
- **I need two or more resilience or infrastructure primitives in the same async service**: pick grelmicro. One toolkit replaces four or five config dialects.
- **I need to schedule background work that survives process restart, retries on failure, and runs across many workers**: pick a task queue (Celery, Dramatiq, ARQ). grelmicro's `Tasks` runs in-process periodic work, not durable jobs.
- **I need long-running workflows with checkpoints, branches, or human-in-the-loop steps**: pick a workflow engine (Temporal, Prefect, Dagster). grelmicro does not orchestrate workflows.
- **I need a web framework**: pick FastAPI, Starlette, Litestar. grelmicro plugs into them, it does not replace them.

## Quick comparison

| If you only need this | Use this | Pick grelmicro when you also need |
|---|---|---|
| Async cache decorator over Redis | [`aiocache`](https://github.com/aio-libs/aiocache) or [`fastapi-cache`](https://github.com/long2ice/fastapi-cache) | Stampede protection (`lock=True` folds misses across replicas, `early=` XFetch refresh), type-safe `TTLCache[T]`, Postgres backend |
| FastAPI rate limiter | [`slowapi`](https://github.com/laurentS/slowapi) or [`fastapi-limiter`](https://github.com/long2ice/fastapi-limiter) | Sliding-window algorithm, structured `RateLimitResult` for retry-after headers, swap Memory and Redis with the same API |
| Async circuit breaker | [`pybreaker`](https://github.com/danielfm/pybreaker) or [`aiobreaker`](https://github.com/arlyon/aiobreaker) | Reconfigurable thresholds, frozen Pydantic config, structured logging context, fleet-wide state on Redis or Postgres |
| Retry with `@retry(stop=, wait=)` | [`tenacity`](https://github.com/jd/tenacity) | A `Retry` that shares the same config + reconfigure shape as the rest of grelmicro |
| Redis distributed lock | [`aioredlock`](https://github.com/joanvila/aioredlock) | Same Lock primitive across Redis, PostgreSQL, SQLite, Kubernetes, in-memory. Adapter-agnostic protocol |
| `/healthz` endpoint | hand-rolled FastAPI handler | Concurrent check execution, per-check TTL cache, `/livez` + `/readyz` + `/healthz` triple, critical vs non-critical, FastAPI router included |

The pattern across every row: pick the focused library when you only need that one Pattern. Pick grelmicro when you need two or more, with a single config and lifecycle story across all of them.

## The unique value: one toolkit, every pattern

What you get from a single import:

| Pattern | grelmicro class | Backends |
|---|---|---|
| Distributed lock | `Lock` | Redis, PostgreSQL, SQLite, Kubernetes Lease, Memory |
| Leader election | `LeaderElection` | same as `Lock` |
| Scheduled-task lock | `TaskLock` | same as `Lock` |
| Cache decorator + TTL store | `Cache`, `TTLCache[T]`, `@cached` | Redis, Memory, Postgres |
| Rate limiter | `RateLimiter` (token bucket, sliding window) | Redis, Memory, Postgres, SQLite |
| Circuit breaker | `CircuitBreaker` | Redis, Memory, Postgres |
| Retry | `Retry`, `@retry` | n/a (in-process) |
| Health checks | `HealthChecks` + `/livez` `/readyz` `/healthz` router | n/a |
| Scheduled tasks | `Tasks`, `TaskRouter`, `@interval` | n/a |
| Structured logging | `grelmicro.log` (JSON, LOGFMT, PRETTY, AUTO) | n/a |
| Tracing | `grelmicro.trace` (`@instrument`, `span`, `add_context`) | OpenTelemetry |

All of them share:

- **One config contract.** Every Pattern reads `GREL_{PATTERN}_{NAME}_*` env vars, or accepts a kwargs constructor, or accepts a Pydantic `Config`. Three paths, identical shape across Patterns.
- **One lifecycle.** `async with Grelmicro(uses=[...]):` opens every backend, every component, every Pattern. No per-library `startup()`/`shutdown()` ceremony.
- **One Redis / Postgres / SQLite client.** Cache, Lock, RateLimiter on the same Redis don't open three connections. The shared client is a Provider. Patterns receive Adapters.
- **One reconfigure protocol.** Every Pattern that owns runtime thresholds (`CircuitBreaker`, `RateLimiter`, `Retry`, `Lock`) supports `reconfigure(new_config)` without restart.
- **One logging context.** Every Pattern logs through the same structured format, with the same field names.

No focused library in the Python ecosystem covers more than two of these Patterns. The closest thing in other ecosystems is the resilience-pipeline class of libraries (Java, .NET), and none of those bundle distributed locks, caches, leader election, scheduled tasks, and health checks into the same toolkit.

If you build microservices on FastAPI today, grelmicro is the missing batteries.

## Cache

`@cached` decorator + `TTLCache` over Redis or in-memory.

| Axis | [`aiocache`](https://github.com/aio-libs/aiocache) | [`fastapi-cache`](https://github.com/long2ice/fastapi-cache) | grelmicro `Cache` |
|---|---|---|---|
| Backends | Memory, Redis, Memcached | Memory, Redis, Memcached, DynamoDB | Memory, Redis, Postgres |
| Decorator | `@cached` | `@cache` | `@cached` |
| Type-safe `Cache[T]` | no | no | yes (`TTLCache[T]` plus `PydanticSerializer(T)`) |
| Stampede protection | local lock via `lock_value` | none | `lock=True` (folds across replicas when a `Coordination` backend is set), `lock="local"` (in-process only), `early=` (XFetch refresh) |
| Serializers | several built-in | json, binary | `JsonSerializer`, `PydanticSerializer`, `PickleSerializer` |
| FastAPI integration | manual | first-class | works with any async framework, no FastAPI coupling |

Pick `aiocache` if you only need the cache primitive and want Memcached. Pick `fastapi-cache` if you want the most FastAPI-native surface and DynamoDB. Pick grelmicro when you also need a distributed Lock, RateLimiter, or CircuitBreaker on the same Redis client and want one config story across all of them.

## Rate Limiter

Cap requests per window with token bucket or sliding window, per key.

| Axis | [`slowapi`](https://github.com/laurentS/slowapi) | [`fastapi-limiter`](https://github.com/long2ice/fastapi-limiter) | [`aiolimiter`](https://github.com/mjpieters/aiolimiter) | grelmicro `RateLimiter` |
|---|---|---|---|---|
| Algorithm | fixed/sliding window | fixed window | token bucket | token bucket, sliding window |
| Backends | Memory, Redis, Memcached, MongoDB | Redis only | in-process only | Memory, Redis, Postgres, SQLite |
| Async-first | partial (Flask-Limiter port) | yes | yes | yes |
| Result shape for retry-after | string parsing | string parsing | none | `RateLimitResult(allowed, limit, remaining, retry_after, reset_after)` |
| Reconfigurable at runtime | no | no | no | yes (`reconfigure(new_config)`) |
| FastAPI integration | first-class | first-class | manual | works in any async framework |

Pick `slowapi` if you want fixed-window per-route limiting on FastAPI and a wider backend choice. Pick `aiolimiter` for the smallest in-process token bucket. Pick grelmicro when you also need a distributed Lock or Cache on the same backend, when you need precise sliding-window semantics, or when you need a structured retry-after result without parsing strings.

## Circuit Breaker

Half-open / open / closed states, with thresholds and ignore lists.

| Axis | [`pybreaker`](https://github.com/danielfm/pybreaker) | [`aiobreaker`](https://github.com/arlyon/aiobreaker) | grelmicro `CircuitBreaker` |
|---|---|---|---|
| Async-first | sync-first, has async wrapper | yes | yes |
| Storage | in-memory, Redis (via plugin) | in-memory | in-memory, Redis, Postgres (fleet-wide state) |
| Decorator + async CM | decorator + sync CM | decorator + async CM | decorator + async CM |
| Frozen Pydantic config | no | no | yes (`CircuitBreakerConfig`) |
| Live reconfigure | no | no | yes (`reconfigure(new_config)`) |
| Structured logging context | no | no | yes (the breaker logs name, state, last_error) |
| Listener hooks | yes (`add_listener`) | basic | structured logs are first-class (name, state, last_error) |

Pick `pybreaker` if you have a sync-first codebase or want listener hooks today. Pick `aiobreaker` if you want a small async-only breaker with no other dependencies. Pick grelmicro when you also need a Retry or Lock that shares the same config story, or when you need to swap thresholds at runtime without restart.

## Retry

Decorator and async context manager with stop / wait / retry conditions.

| Axis | [`tenacity`](https://github.com/jd/tenacity) | [`backoff`](https://github.com/litl/backoff) | grelmicro `Retry` |
|---|---|---|---|
| Stop conditions | rich (`stop_after_attempt`, `stop_after_delay`, ...) | basic | `attempts=` (count) and `max_seconds=` (time budget), whichever comes first. |
| Wait strategies | rich (`wait_exponential`, `wait_chain`, ...) | exponential, fibonacci, constant | exponential, constant, linear, fibonacci, random |
| Retry condition | `retry_if_*` factories, `\|`/`&` operators | exception type or predicate | `Match.exception(...)`, `Match.result(...)`, `Match.exception_message(...)`, `Match.exception_cause(...)`, plus `not_*` twins, `\|`/`&` operators |
| Async support | yes | yes | yes |
| Sync support | yes | yes | yes (decorator works on both) |
| Frozen config + reconfigure | no | no | yes |
| Shares filter DSL with other resilience Patterns | n/a | n/a | yes (`Match` ships with `Retry`, reused across the resilience module) |

`tenacity` is the right pick for the most advanced stop and wait vocabulary. Pick grelmicro `Retry` when you want a smaller surface, result-based retry out of the box (`Match.result(None)`), and a filter DSL (`Match`) shared with the other resilience Patterns in the same library.

## Distributed Lock

Mutex across processes, replicas, or hosts.

| Axis | [`aioredlock`](https://github.com/joanvila/aioredlock) | [`redis-py` lock](https://redis.readthedocs.io/en/latest/commands.html#redis.commands.core.CoreCommands.lock) | hand-rolled Postgres advisory lock | grelmicro `Lock` |
|---|---|---|---|---|
| Algorithm | Redlock | SET NX with Lua release | `pg_advisory_xact_lock` | SET NX + Lua release in Redis, advisory locks in Postgres, `coordination.k8s.io/v1` Lease with resourceVersion CAS in Kubernetes |
| Backends | Redis only | Redis only | Postgres only | Redis, Postgres, SQLite, Kubernetes, Memory |
| Async-first | yes | partial | depends on driver | yes |
| Token-based release | yes | yes | n/a (transaction-scoped) | yes (token derived from worker id + task or thread id, deterministic so re-acquire extends the lease) |
| Reentrant detection | manual | no | n/a | yes (raises `LockReentrantError`) |
| Idempotent acquire (lease extension) | partial | manual | n/a | yes (the same token re-acquire extends the lease) |
| `from_thread` adapter for blocking code | no | no | n/a | yes |
| Reconfigurable lease and retry | no | no | n/a | yes |
| Same primitive backs leader election and task lock | no | no | n/a | yes (`LeaderElection` and `TaskLock` share the protocol) |

Pick `aioredlock` if you want the Redlock algorithm specifically. Pick a hand-rolled `pg_advisory_lock` if Postgres is your only backend and you only need one lock kind. Pick grelmicro when you want one Lock primitive across multiple backend choices, plus `LeaderElection` and `TaskLock` on the same protocol.

## Health Checks

Liveness, readiness, and aggregate endpoints for orchestrators and load balancers.

| Axis | hand-rolled FastAPI handler | [`py-healthcheck`](https://pypi.org/project/py-healthcheck/) | grelmicro `HealthChecks` |
|---|---|---|---|
| Concurrent check execution | manual `asyncio.gather` | no | yes (`asyncio.TaskGroup`) |
| Per-check timeout | manual | no | yes |
| Per-check TTL cache + single-flight | manual | no | yes (default `cache_ttl=1.0`) |
| Critical vs non-critical | manual | no | yes (non-critical never flips `/readyz`) |
| `/livez` + `/readyz` + `/healthz` triple | hand-rolled | no | yes (FastAPI router included) |
| `?exclude` query | manual | no | yes |
| Verbose details gated by `Depends(...)` | manual | no | yes (`show_details=Depends(fn)`) |

Pick a hand-rolled handler if you only need one boolean endpoint. Pick grelmicro when you want the orchestrator-grade triple (`/livez`, `/readyz`, `/healthz`) with concurrent execution, caching, and details gated by a FastAPI dependency.

## What grelmicro is NOT

A few categories the comparison page does not cover, because grelmicro does not compete in them:

| Category | Use this instead |
|---|---|
| HTTP framework | [FastAPI](https://fastapi.tiangolo.com/), [Starlette](https://www.starlette.io/), [Litestar](https://litestar.dev/) |
| Embedded server | [Uvicorn](https://www.uvicorn.org/), [Hypercorn](https://github.com/pgjones/hypercorn), [Granian](https://github.com/emmett-framework/granian) |
| Message broker abstraction | [FastStream](https://faststream.airt.ai/), [aio_pika](https://github.com/mosquito/aio-pika) |
| Background workers (queues) | [Celery](https://docs.celeryq.dev/), [dramatiq](https://github.com/Bogdanp/dramatiq), [taskiq](https://github.com/taskiq-python/taskiq) |
| ORM | [SQLAlchemy](https://www.sqlalchemy.org/), [SQLModel](https://sqlmodel.tiangolo.com/), [tortoise-orm](https://tortoise.github.io/) |
| Auth | [Authlib](https://authlib.org/), [authx](https://github.com/yezz123/authx), [fastapi-users](https://github.com/fastapi-users/fastapi-users) |
| Service mesh / discovery | [Istio](https://istio.io/), [Linkerd](https://linkerd.io/), Kubernetes DNS, [Consul](https://www.consul.io/) |
| API gateway | [Envoy](https://www.envoyproxy.io/), [Nginx](https://nginx.org/), [Kong](https://konghq.com/) |

grelmicro fills the gap between "I picked FastAPI for HTTP" and "I need a real distributed lock, rate limit, circuit breaker, cache, leader election, scheduled tasks, and health checks". It does not try to replace the rows above.
