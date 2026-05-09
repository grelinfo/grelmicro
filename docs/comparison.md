# Comparison

How grelmicro compares to the focused libraries that cover one Pattern each. Every comparison is per-domain. Read the row that matches what you're picking today.

Tone: factual. Where the alternative is the better fit, this page says so.

## TL;DR

| If you only need this | Use this | Pick grelmicro when you also need |
|---|---|---|
| Async cache decorator over Redis | [`aiocache`](https://github.com/aio-libs/aiocache) or [`fastapi-cache`](https://github.com/long2ice/fastapi-cache) | Opt-in per-key stampede protection (`lock=True`), type-safe `TTLCache[T]`, Postgres backend (planned, see [#167](https://github.com/grelinfo/grelmicro/issues/167)) |
| FastAPI rate limiter | [`slowapi`](https://github.com/laurentS/slowapi) or [`fastapi-limiter`](https://github.com/long2ice/fastapi-limiter) | GCRA algorithm, structured `RateLimitResult` for retry-after headers, swap Memory and Redis with the same API |
| Async circuit breaker | [`pybreaker`](https://github.com/danielfm/pybreaker) or [`aiobreaker`](https://github.com/arlyon/aiobreaker) | Reconfigurable thresholds, frozen Pydantic config, structured logging context, distributed backend (planned, see [#163](https://github.com/grelinfo/grelmicro/issues/163)) |
| Retry with `@retry(stop=, wait=)` | [`tenacity`](https://github.com/jd/tenacity) | A `Retry` that shares the same config + reconfigure shape as the rest of grelmicro (ships with [#165](https://github.com/grelinfo/grelmicro/issues/165)) |
| Redis distributed lock | [`aioredlock`](https://github.com/joanvila/aioredlock) | Same Lock primitive across Redis, PostgreSQL, SQLite, Kubernetes, in-memory. Adapter-agnostic protocol |
| `/healthz` endpoint | hand-rolled FastAPI handler | Concurrent check execution, per-check TTL cache, `/livez` + `/readyz` + `/healthz` triple, critical vs non-critical, FastAPI router included |

The pattern across every row: pick the focused library when you only need that one Pattern. Pick grelmicro when you also need at least one other Pattern from the same toolkit, with a single config and lifecycle story.

## Cache

`@cached` decorator + `TTLCache` over Redis or in-memory.

| Axis | [`aiocache`](https://github.com/aio-libs/aiocache) | [`fastapi-cache`](https://github.com/long2ice/fastapi-cache) | grelmicro `Cache` |
|---|---|---|---|
| Backends | Memory, Redis, Memcached | Memory, Redis, Memcached, DynamoDB | Memory, Redis (Postgres at 1.0, see [#167](https://github.com/grelinfo/grelmicro/issues/167)) |
| Decorator | `@cached` | `@cache` | `@cached` |
| Type-safe `Cache[T]` | no | no | yes (`TTLCache[T]` plus `PydanticSerializer(T)`) |
| Per-key stampede protection | local lock via `lock_value` | none | opt-in via `lock=True` (off by default). Distributed lock planned (see [#235](https://github.com/grelinfo/grelmicro/issues/235)) |
| Serializers | several built-in | json, binary | `JsonSerializer`, `PydanticSerializer`, `PickleSerializer` |
| FastAPI integration | manual | first-class | works with any async framework, no FastAPI coupling |

Pick `aiocache` if you only need the cache primitive and want Memcached. Pick `fastapi-cache` if you want a FastAPI-native API surface. Pick grelmicro when you also need a distributed Lock or CircuitBreaker on the same Redis client and want one config story across all of them.

## Rate Limiter

Cap requests per window with token bucket or GCRA, per key.

| Axis | [`slowapi`](https://github.com/laurentS/slowapi) | [`fastapi-limiter`](https://github.com/long2ice/fastapi-limiter) | [`aiolimiter`](https://github.com/mjpieters/aiolimiter) | grelmicro `RateLimiter` |
|---|---|---|---|---|
| Algorithm | fixed/sliding window | fixed window | token bucket | token bucket, GCRA |
| Backends | Memory, Redis, Memcached, MongoDB | Redis only | in-process only | Memory, Redis (Postgres + SQLite at 1.0, see [#164](https://github.com/grelinfo/grelmicro/issues/164), [#173](https://github.com/grelinfo/grelmicro/issues/173)) |
| Async-first | partial (Flask-Limiter port) | yes | yes | yes |
| Result shape for retry-after | string parsing | string parsing | none | `RateLimitResult(allowed, limit, remaining, retry_after, reset_after)` |
| Reconfigurable at runtime | no | no | no | yes (`reconfigure(new_config)`) |
| FastAPI integration | first-class | first-class | manual | works in any async framework |

Pick `slowapi` if you want fixed-window per-route limiting on FastAPI. Pick `aiolimiter` for the smallest in-process token bucket. Pick grelmicro when you also need a distributed Lock or Cache on the same backend, or when you need GCRA semantics and a structured retry-after result.

## Circuit Breaker

Half-open / open / closed states, with thresholds and ignore lists.

| Axis | [`pybreaker`](https://github.com/danielfm/pybreaker) | [`aiobreaker`](https://github.com/arlyon/aiobreaker) | grelmicro `CircuitBreaker` |
|---|---|---|---|
| Async-first | sync-first, has async wrapper | yes | yes |
| Storage | in-memory, Redis (via plugin) | in-memory | in-memory (Memory backend), distributed protocol at 1.0 (see [#163](https://github.com/grelinfo/grelmicro/issues/163)) |
| Decorator + async CM | decorator + sync CM | decorator + async CM | decorator + async CM |
| Frozen Pydantic config | no | no | yes (`CircuitBreakerConfig`) |
| Live reconfigure | no | no | yes (`reconfigure(new_config)`) |
| Structured logging context | no | no | yes (the breaker logs name, state, last_error) |
| Listener hooks | yes (`add_listener`) | basic | structured logs are first-class, explicit listener API post-1.0 |

Pick `pybreaker` if you have a sync-first codebase. Pick `aiobreaker` if you want a small async-only breaker with no other dependencies. Pick grelmicro when you also need a Retry or Lock that shares the same config story, or when you need to swap thresholds at runtime without restart.

## Retry

Decorator and async context manager with stop / wait / retry conditions.

| Axis | [`tenacity`](https://github.com/jd/tenacity) | [`backoff`](https://github.com/litl/backoff) | grelmicro `Retry` (ships with [#165](https://github.com/grelinfo/grelmicro/issues/165)) |
|---|---|---|---|
| Stop conditions | rich (`stop_after_attempt`, `stop_after_delay`, `stop_when_event_set`, ...) | basic | basic, with the same shape as Polly's `RetryStrategyOptions` |
| Wait strategies | rich (`wait_exponential`, `wait_random_exponential`, `wait_chain`, ...) | exponential, fibonacci, constant | exponential, fixed, jittered |
| Retry condition | `retry_if_exception_type`, `retry_if_result`, full predicate | exception type or predicate | exception type, predicate |
| Async support | yes | yes | yes |
| Sync support | yes | yes | yes (decorator works on both) |
| Frozen config + reconfigure | no | no | yes |
| Composes with the rest of the library | no | no | yes (one config story across `Retry`, `CircuitBreaker`, `Lock`, ...) |

`tenacity` is the right pick for advanced retry policies. Its stop / wait / retry vocabulary is unmatched. Pick grelmicro `Retry` when you want a smaller surface that shares config and reconfigure ergonomics with the rest of your toolkit.

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

Pick `aioredlock` if you want the Redlock algorithm specifically. Pick a hand-rolled `pg_advisory_lock` if Postgres is your only backend and you only need one lock kind. Pick grelmicro when you want one Lock primitive across multiple backend choices, with a `LeaderElection` and `TaskLock` on the same protocol.

## Health Checks

Liveness, readiness, and aggregate endpoints for orchestrators and load balancers.

| Axis | hand-rolled FastAPI handler | [`py-healthcheck`](https://pypi.org/project/py-healthcheck/) | grelmicro `HealthChecks` |
|---|---|---|---|
| Concurrent check execution | manual `asyncio.gather` | no | yes (`asyncio.TaskGroup`) |
| Per-check timeout | manual | no | yes |
| Per-check TTL cache + single-flight | manual | no | yes (default `cache_ttl=1.0`) |
| Critical vs non-critical | manual | no | yes (non-critical never flips `/readyz`) |
| `/livez` + `/readyz` + `/healthz` triple | hand-rolled | no | yes (z-pages convention, FastAPI router included) |
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

grelmicro fills the gap between "I picked FastAPI for HTTP" and "I need a real distributed lock, rate limit, circuit breaker, leader election". It does not try to replace the rows above.

## Cross-language anchors

Where the corresponding pattern set lives in other ecosystems, for vocabulary alignment:

| Ecosystem | Pattern set |
|---|---|
| Java | [Resilience4j](https://github.com/resilience4j/resilience4j) (resilience), [Redisson](https://github.com/redisson/redisson) (distributed primitives), [ShedLock](https://github.com/lukas-krecan/ShedLock) (scheduled-task locks), [Spring Boot Actuator](https://docs.spring.io/spring-boot/reference/actuator/index.html) (health) |
| .NET | [Polly](https://github.com/App-vNext/Polly) v8 (resilience pipelines), `IDistributedLock` ecosystem |
| Go | [`sony/gobreaker`](https://github.com/sony/gobreaker) (circuit breaker), [`golang.org/x/sync/singleflight`](https://pkg.go.dev/golang.org/x/sync/singleflight) (cache stampede) |
| Node | [`opossum`](https://github.com/nodeshift/opossum) (circuit breaker), [`bull`](https://github.com/OptimalBits/bull) (Redis-backed queue + lock) |

This page does not compare grelmicro line-by-line to non-Python projects. The cross-language anchors are here to confirm the vocabulary is shared. Resilience4j and Polly informed the Pattern selection.
