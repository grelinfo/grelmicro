# Grelmicro Market Analysis

A competitive review of grelmicro 0.26.0 against the leading libraries in each
pattern it covers. The goal is to find what grelmicro is missing and what it can
do better before 1.0.

Research date: June 2026. Star and download figures are approximate and taken
from GitHub and PyPI at the time of search. Treat them as orders of magnitude.

**Status update (2026-06-07).** Since this analysis: `grelmicro.sync` was unified
into `grelmicro.coordination` (one package, one `Coordination` component), and the
cache stampede API moved from the `stampede=` menu to a simpler `lock=` argument.
The top gaps below are now **closed**: fencing tokens (strictly monotonic on every
backend, Kubernetes via expire-in-place), a full metrics / Prometheus layer
(OpenTelemetry-based, with complete auto-instrumentation), and cache tags plus
`get_or_set` and batch operations. The remaining headline gap is the
interval-only scheduler (no cron / calendar).

## Summary

Grelmicro is a single async-first toolkit covering coordination (locks, leader
election), resilience (retry, circuit breaker, rate limiter, timeout, bulkhead,
fallback, shield), caching, task scheduling, logging, tracing, and health
checks. In Python no single library covers this scope. The natural alternative
is a stack of six to eight separate libraries glued together.

That breadth is the moat. The risk is that any one module is judged against a
specialist that does that one thing better. This document maps grelmicro module
by module against those specialists.

The three highest-leverage gaps this analysis found, and their status:

1. **Metrics — shipped.** The `Metrics` component now provides an OpenTelemetry
   meter with a Prometheus `/metrics` router and complete auto-instrumentation
   across health, resilience, and tasks. All three observability pillars covered.
2. **Fencing tokens — shipped.** `Lock` now returns a strictly monotonic
   `fencing_token` per name on every backend, closing the classic distributed-lock
   correctness hole.
3. **Scheduler is interval-only — open.** No cron, no calendar, no
   run-once-at-time. The single biggest reason a user would pick APScheduler. This
   is now the top remaining gap.

## Positioning at a glance

| Module | Real peers | Grelmicro standing |
| --- | --- | --- |
| Resilience | resilience4j, Polly, Failsafe (cross-language), tenacity, stamina, pybreaker, limits | Only Python lib with the full suite. Behind the gold standards on hedging, jitter, metrics |
| Locks / leader election | sherlock, pottery, aioredlock, kazoo, redis-py | Widest async backend matrix incl. K8s Lease. Now ships fencing tokens. Still missing semaphore, RW locks |
| Caching | cashews, aiocache, dogpile.cache | Strong stampede story, now with tags, batch ops, and get_or_set. Still missing soft TTL |
| Task scheduling | APScheduler v4, rocketry, schedule, aiocron | Unique distributed-safe + FastAPI-native. Missing cron and calendar triggers |
| Observability + DI | structlog, opentelemetry, prometheus_client, dishka, svcs | Best-in-class health checks, now with a full OpenTelemetry metrics + Prometheus layer |

---

## 1. Resilience

### Competitors

| Library | Language | Category | Maturity | Note |
| --- | --- | --- | --- | --- |
| tenacity | Python | Retry | ~8.6k, very mature | De-facto retry library, async, result predicates, stats |
| stamina | Python | Retry | ~1.4k, active | Production wrapper over tenacity, jitter, Prometheus, test toggles |
| pybreaker | Python | Circuit breaker | ~677, mature | Redis backend, half-open success threshold, listeners |
| purgatory | Python | Circuit breaker | small, active | async/sync, in-memory or Redis, event hooks |
| limits | Python | Rate limiter | mature | Fixed/moving/sliding window, Redis/Memcached/Mongo |
| pyrate-limiter | Python | Rate limiter | active | Leaky bucket, multi-backend, cost per request |
| resilience4j | Java | Full suite | gold standard | Time + count windows, event streams, Micrometer metrics |
| Polly | .NET | Full suite | gold standard | Composable pipelines, hedging, chaos (Simmy) |
| Failsafe | Java | Full suite | mature | Zero-dep policies, hedging, fallback chains |
| Alibaba Sentinel | Java/Go | Flow control | mature | Slow-call ratio breaking, adaptive system protection |

No Python competitor matches grelmicro's scope. Its true peers are Java and .NET.

### What grelmicro does well

- **The bundled Shield is genuinely novel.** No Python competitor ships an
  all-in-one composite (timeout + retry + adaptive rate limiter + cache +
  fallback) as one primitive. Polly pipelines and resilience4j decorator chains
  are the closest, but both require manual composition.
- **Fleet-wide state for both breaker and limiter** across Redis, Postgres,
  SQLite, and memory. No other Python library unifies both patterns under one
  backend abstraction.
- **Adaptive CUBIC rate limiter.** Most Python limiters are static. An adaptive
  limiter that probes for capacity is unusual and on-trend.
- **Async-first and runtime-reconfigurable with Pydantic-validated config.**

### Gaps, by impact

1. **Hedging.** Polly's flagship feature, also in Failsafe. Latency mode (fire a
   second attempt if the first is slow, take the fastest), parallel mode, and
   sequential-on-failure mode. The biggest single gap versus the gold standards.
2. **Decorrelated jitter.** Grelmicro lists exponential, constant, linear,
   fibonacci, and random backoff but not decorrelated, full, or equal jitter.
   Decorrelated jitter is the recommended default for thundering-herd avoidance
   and users look for it by name.
3. **Circuit breaker windows.** resilience4j supports both count-based and
   time-based sliding windows. Sentinel trips on slow-call ratio and error
   ratio, not just a failure count. Add failure-rate and slow-call-rate
   thresholds and a time window.
4. **Half-open probe config.** Configurable number of probe calls and required
   consecutive successes before closing (resilience4j and pybreaker both expose
   this).
5. **First-class metrics and event hooks.** resilience4j, Polly, and stamina all
   treat metrics as table stakes. A documented OpenTelemetry-native hook contract
   for state transitions, retries, rejections, trips, and latencies. See the
   cross-cutting metrics gap in section 5.
6. **Retry budgets.** Cap the ratio of retries to original calls so retries
   cannot amplify an outage. No Python library has this, and grelmicro's
   distributed backends make a fleet-wide budget uniquely feasible. A
   differentiator, not just a catch-up.
7. **Bulkhead with bounded queue.** Today it is a concurrency cap. resilience4j
   adds a wait queue with a max wait duration (reject versus wait).
8. **Chaos / fault injection** that composes into Shield (the Polly plus Simmy
   model). On-brand for a resilience-testing story.
9. **Test-mode toggles** to disable retries and strip backoff in test suites, a
   la stamina, using the existing runtime reconfiguration.
10. **Confirm parity** on result-based retry predicates (retry on a returned
    value, not only on exceptions) and ordered fallback chains with per-exception
    routing.

---

## 2. Coordination: locks and leader election

### Competitors

| Library | Backends | Async | Note |
| --- | --- | --- | --- |
| sherlock | File, Redis, Memcached, etcd, K8s | No | Closest analogue, but sync and dormant |
| pottery | Redis, Redlock | Yes | Redlock plus semaphore and NextID |
| aioredlock | Redis multi-master | Yes | asyncio Redlock with auto-extension watchdog |
| python-redis-lock | Redis | No | Fair FIFO via BLPOP, auto_renewal |
| redis-py native Lock | Redis | Yes | blocking_timeout, extend, reacquire |
| kazoo | ZooKeeper | No | Full recipe set: RW locks, semaphore, election, barriers |
| etcd3 clients | etcd | Mixed | Native revisions, lease-based locks |
| filelock | Local FS | Async wrappers | ~522M downloads/mo, single-host only |

No competitor combines async, a wide backend matrix, and bundled leader election
the way grelmicro does.

### What grelmicro does well

- **Async-first on every backend.** Most multi-backend options are sync.
- **Widest backend matrix in one library** including Kubernetes Lease as a
  first-class backend. For teams on Kubernetes this means no extra Redis, etcd,
  or ZooKeeper.
- **Leader election bundled and polished**, with a renewal loop, wait helpers,
  and `is_leader_confirmed_within` as a recency guard against stale leadership.
- **TaskLock with min/max hold times** is a domain-specific primitive no
  competitor offers.

### Gaps, by impact

1. **Fencing tokens.** This is the headline correctness gap. Tokens are random
   identity strings used only to match owner on release. They are not
   monotonically increasing, so a guarded resource cannot reject a stale holder
   after a GC pause, network partition, or lease expiry (the Kleppmann critique).
   Issue a monotonic token on acquire and renewal, backed per-backend by a
   Postgres sequence, Redis INCR, etcd revision, or a K8s Lease counter. No
   mainstream Python lock library does this. It would turn "single-leader not
   guaranteed" into "single-leader provable at the resource."
2. **Bounded blocking acquire.** Today there is acquire (retries forever) and
   acquire_nowait. Add `acquire(timeout=...)` that retries to a deadline then
   fails. redis-py, python-redis-lock, sherlock, and kazoo all have this.
3. **Auto-renewal watchdog** on Lock and TaskLock for long critical sections,
   reusing the renewal loop that LeaderElection already runs internally.
4. **Distributed semaphore (N holders).** The most-requested missing primitive.
   kazoo and pottery both ship one.
5. **Read-write locks** for read-heavy shared resources (kazoo, filelock SQLite).
6. **Fairness.** Retry-polling is not FIFO and can starve under contention.
   Document the tradeoff, and consider an optional fair queue on Redis.
7. **Consider an etcd backend.** It is the natural correctness-grade consensus
   backend, where fencing tokens (revisions) are native.

---

## 3. Caching

### Competitors

| Library | Backends | Async | Note |
| --- | --- | --- | --- |
| cashews | Memory, Redis, Disk | Yes | Gold-standard async cache, the study target |
| aiocache | Memory, Redis, Memcached | Yes | Multi-backend manager, multi_cached, plugins |
| dogpile.cache | Memcached, Redis, dbm | No | The original dogpile stampede lock, regions |
| fastapi-cache2 | Redis, Memcached, memory | Yes | Response caching with ETag and 304 |
| cachetools | In-memory | No | De-facto memoizing collections, ~46M weekly |
| diskcache | Disk | No | Process-safe disk cache, stampede prevention |

### What grelmicro does well

- **Stampede protection.** A single `lock=True` folds concurrent misses and
  picks the cross-replica path automatically when a `Coordination` backend is
  configured, in-process otherwise (`lock="local"` forces in-process). This
  matches cashews and cachebox ergonomics while still folding misses across
  replicas, which most Python libs do not do at all.
- **XFetch / probabilistic early recompute** (`early=`) is a correct, principled
  refresh-ahead that cashews only approximates.
- **Postgres cache backend** for teams who do not want to run Redis. cashews and
  aiocache do not offer this.
- **Pydantic-validated typed cache** with a first-class PydanticSerializer.
- **DI-based override** for tests, cleaner than the global-singleton setup model.

### Gaps, by impact

Tier 1, parity with cashews and aiocache:

1. **Tag-based invalidation.** `tags=` on cache and decorator, plus
   `delete_tags(...)`, backed by a Redis SET (and a Postgres join table) so
   invalidation is O(tag), not a key scan. The single biggest gap.
2. **get_or_set on TTLCache**, atomic get-or-compute reusing the stampede
   machinery.
3. **Batch ops** (get_many, set_many, delete_many) mapped to Redis MGET and
   pipelines and a single Postgres statement.
4. **Namespace or prefix delete** so a tenant or feature can be flushed without
   clearing everything.

Tier 2, plays to grelmicro's strengths:

5. **Soft TTL and stale-on-error failover** (serve last good value when the
   function raises). Pairs naturally with XFetch.
6. **Two-tier L1 plus L2 cache** (memory in front of Redis or Postgres). The
   memory backend and TTLCache abstraction already exist, so composing them would
   beat every Python competitor except cashews' client-side mode.
7. **Metrics hook**, exposing `cache_info()` as Prometheus counters. See section 5.

Tier 3, breadth:

8. **Key templating** (`key="user:{user.id}"`) as sugar over key_maker.
9. **TTL string and timedelta** acceptance (`"2h5m"`).
10. **Ship the SQLite backend** already on the roadmap.

Skip rate-limit and circuit-breaker-as-cache-decorators. Those belong in the
resilience module, not the cache.

---

## 4. Task scheduling

Grelmicro's Tasks is an in-process async scheduler, not a distributed queue. It
is **not** competing with Celery, taskiq, dramatiq, arq, or faststream. Its real
peers are APScheduler v4, rocketry, schedule, aiocron, and asyncz. Keep that
distinction sharp on the comparison page.

### Competitors (peer set)

| Library | Model | Async | Note |
| --- | --- | --- | --- |
| APScheduler v4 | In-process + shared store | Async-first | The reference scheduler, cron + calendar + date triggers, persistence |
| schedule | In-process, in-memory | No | Human-friendly, no persistence, no async |
| rocketry | In-process, condition-based | Yes | Cron plus logical conditions, now largely unmaintained |
| aiocron | In-process cron decorator | Yes | Tiny crontab decorator for asyncio |
| asyncz | In-process scheduler | Async-first | date/interval/cron, multiple stores, ASGI-friendly |
| procrastinate / pgqueuer | Postgres-backed queue | Yes | No broker, periodic tasks, retries, DB locks. Philosophical neighbors |

### What grelmicro does well

- **Distributed coordination with zero broker.** `max_lock_seconds` gives
  at-most-once-per-interval across replicas and `leader=` gives leader-only
  execution, both as one-line decorator args. This is the standout. Running an
  APScheduler or schedule app on multiple replicas normally fires the job on
  every pod. Grelmicro's wedge is "the in-process scheduler that is safe to run
  on N replicas."
- **FastAPI-router-style TaskRouter** with include_router composition. No peer
  offers this idiom.
- **Drops straight into FastAPI lifespan**, no worker process, no broker, no CLI,
  DI via fast-depends matching FastAPI's Depends.
- **Production-grade graceful shutdown** with a K8s-aligned shutdown_timeout that
  drains in-flight work.

### Gaps, by impact

Tier 1, scheduling expressiveness (this is what peers have and grelmicro does not):

1. **Cron trigger** (`@tasks.cron("*/5 * * * *", tz=...)`). The most-requested
   capability of any scheduler and the main reason to pick APScheduler. Carry the
   existing lock and leader story through, so distributed-safe cron with zero
   broker becomes a feature no peer has.
2. **Calendar and one-shot triggers** ("every day at 02:00", "run once at T"),
   which forces timezone awareness alongside.

Tier 2, robustness knobs the interval loop is one step from:

3. **Per-task retry policy** (`retries=`, `retry_backoff=`). Today a failure just
   waits a full interval.
4. **Jitter / splay** on interval and cron. Cheap, and especially valuable
   because grelmicro is multi-instance by design. Even with locking, the winning
   pod stampedes shared resources at the same wall-clock instant.

Tier 3, observability and lifecycle:

5. **Introspection** (last_run, last_status, next_run, success/failure counters),
   which pairs with the health component.
6. **Lifecycle hooks** (on_success, on_error), mirroring APScheduler's listeners.

Tier 4, weigh against scope creep: runtime add/remove of tasks, and an explicit
max_instances or overrun policy (skip versus wait).

Do not build result backends, broker integrations, worker pools, or task
chaining. Those pull grelmicro into the Celery and taskiq category it has
correctly chosen not to enter. Point users there from the docs instead.

---

## 5. Observability and dependency injection

### Competitors

| Area | Leaders |
| --- | --- |
| Logging | loguru (~23k), structlog (~3k), python-json-logger |
| Tracing / metrics | opentelemetry-python, ddtrace, prometheus_client |
| Health checks | fastapi-health, py-healthcheck |
| DI | dependency-injector (~4.9k), dishka (~1.1k), that-depends, svcs, lagom, fast-depends |
| Framework peers | FastAPI, Litestar, FastStream, nameko |

### What grelmicro does well

- **One cohesive toolkit** instead of a pile of libraries. The realistic
  alternative is structlog plus opentelemetry plus prometheus_client plus a
  health lib plus a DI container plus orjson, each with its own config surface.
- **Health checks are best-in-class for Python.** Critical versus non-critical,
  per-check timeout and caching, and Kubernetes-aligned `/livez`, `/readyz`,
  `/healthz` exceed the dedicated health libraries.
- **Runtime-reconfigurable components** (hot reload from a ConfigMap) are
  uncommon. None of the surveyed DI containers advertise this.
- **FastAPI-style ergonomics** across the whole toolkit, built on fast-depends,
  the same engine behind FastStream.

### Gaps, by impact

1. **Metrics are missing entirely.** Confirmed in source: the trace module
   imports only OpenTelemetry traces and OTLP trace exporters. There is no Meter,
   no Counter/Histogram/Gauge, no Prometheus client, and no `/metrics` endpoint.
   This is the single biggest hole versus the "complete observability"
   positioning, because traces plus logs without metrics is two-thirds of the
   three pillars. Add an OTel metrics layer parallel to the trace component, a
   Prometheus `/metrics` endpoint mirroring `health_router()`, and per-check
   health gauges. This finding also drives the resilience and cache metrics gaps.
2. **Logging extensibility.** Add a structlog-style user processor chain, a
   standalone contextvars bind for correlation IDs not tied to a span, and a
   redaction or PII-scrubbing processor (a feature even the big logging libraries
   lack, so it differentiates).
3. **Tracing: baggage and optional auto-instrumentation.** Support W3C baggage
   propagation, and either an opt-in auto-instrumentation path or documented
   interop with `opentelemetry-instrument` for HTTP and DB spans.
4. **DI: formalize scopes and testing overrides.** Make request-scope versus
   app-scope explicit and documented (dishka is the bar), confirm generator-style
   resource finalization, and surface the test-override story (fast-depends
   already enables it).
5. **Health: startup-probe state and a structured degraded state**, distinct from
   binary healthy or unhealthy.
6. **Broaden framework integrations** to Litestar and FastStream. FastStream
   shares the fast-depends lineage, so it should be a natural fit.

---

## First public release lens (beta / RC)

This is the first public release. The bar is a perfect first impression, not
feature parity with resilience4j or cashews. None of the gaps above are release
blockers. Shipping any of them rushed would hurt the first impression more than
their absence does. They are the post-1.0 story.

For a first impression, three things matter, in order.

**1. Honest, sharp positioning.** A newcomer decides in thirty seconds whether
the library is for them. The README and docs index must state plainly what
grelmicro is (one async toolkit of microservice patterns with pluggable
backends) and what it is not (not a task queue, not a web framework, not a
Redlock). The comparison page must keep the scheduler-versus-queue and
lock-versus-Redlock lines crisp. Lead with the genuine moats: the bundled
Shield, unified distributed backends, distributed-safe in-process scheduler, K8s
Lease support, best-in-class health checks. Do not imply features that are not
shipped.

**2. Every shipped surface is flawless.** Polish beats breadth on day one.

- Every example in the docs runs as written, against a real backend, with no
  edits. Broken copy-paste is the fastest way to lose trust.
- The public API is consistent across modules: same naming, same Config style,
  same component-wiring idiom, same error types. A user who learns one module
  should predict the next.
- Docstrings and type hints are complete on every public symbol, since editors
  and the rendered API reference are part of the first impression.
- Error messages are actionable (what went wrong, which component, how to fix).
- The getting-started path works end to end in under five minutes, from install
  to a running FastAPI app with one lock and one task.

**3. No honesty gaps in the capability matrix.** The matrix already marks some
cells "Future" (SQLite cache, SQLite circuit breaker). That is fine and honest.
Make sure nothing is marked supported that is not, and that every "Future" cell
has an issue link. A first-time reader trusts the matrix completely, so it must
be exactly right.

What to explicitly NOT do before the first release: do not start hedging,
fencing tokens, cron, or cache tags. Announce them as the roadmap. A clear "here
is what is coming" section sets expectations and signals momentum without
risking a rushed, half-tested feature in the launch build.

Pre-release checklist worth running:

- README and docs index positioning reviewed against the moats above.
- Every doc example executed against its backend.
- Public API surface swept for naming and Config consistency.
- Capability matrix audited cell by cell, every "Future" issue-linked.
- Changelog and migration notes clean and FastAPI-terse.
- Install extras (`redis`, `postgres`, `sqlite`, `kubernetes`, `opentelemetry`,
  `structlog`, `standard`) each verified to install and import.
- A roadmap section published so the feature gaps read as intentional, not
  missing.

## Prioritized roadmap view

Cross-cutting, do these first because they unblock the toolkit's core claims:

1. **Metrics layer plus Prometheus endpoint.** Makes the observability story
   defensible and feeds resilience and cache metrics hooks.
2. **Fencing tokens on locks.** Closes the correctness hole and becomes the only
   Python lock library with first-class fencing.
3. **Cron and calendar triggers.** Closes the clearest scheduler gap, and
   distributed-safe cron with no broker is a feature no peer has.

High-value per module:

4. Hedging and decorrelated jitter (resilience).
5. Cache tags plus batch ops plus get_or_set (caching).
6. Distributed semaphore and bounded blocking acquire (coordination).
7. Per-task retry and jitter (scheduling).

Differentiators worth leading with in marketing, because no Python competitor
has them:

- The bundled Shield composite.
- Unified distributed backends for both circuit breaker and rate limiter.
- Distributed-safe in-process scheduler (lock plus leader plus graceful drain).
- Kubernetes Lease as a first-class lock and leader-election backend.
- Best-in-class Kubernetes health checks.
