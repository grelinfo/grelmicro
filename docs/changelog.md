# Changelog

## Unreleased

### Breaking

* 💥 Rename the `ExternalConfig` `interval` parameter to `reload_interval`, tying the knob to the `reload()` verb it controls. Update `ExternalConfig(interval=...)` to `reload_interval=`.
* 💥 Rename the log dedup `ttl_seconds` field to `ttl`, matching the bare-noun duration convention used everywhere else (the cache `ttl`, lock durations). Update `DuplicateFilter(ttl_seconds=...)` and `DedupConfig` to `ttl=`.
* 💥 Rename the `Trace` component symbols to the `Trace` stem so they match the component name. `TracingConfig`, `TracingError`, `TracingExporterType`, `TracingProcessorType`, `TracingSamplerType`, and `TracingSettingsValidationError` become `TraceConfig`, `TraceError`, `TraceExporterType`, `TraceProcessorType`, `TraceSamplerType`, and `TraceSettingsValidationError`. Update imports.
* 💥 Rename the `Log` component symbols to the `Log` stem so they match the component name. `LoggingConfig`, `LoggingError`, `LoggingBackendType`, `LoggingFormatType`, `LoggingLevelType`, `LoggingSerializerType`, `LoggingTimeZoneType`, and `LoggingSettingsValidationError` become `LogConfig`, `LogError`, `LogBackendType`, `LogFormatType`, `LogLevelType`, `LogSerializerType`, `LogTimeZoneType`, and `LogSettingsValidationError`. Update imports.
* 💥 Rename the concrete leader election adapters to the `*Adapter` suffix every other pattern already uses. `MemoryLeaderElectionBackend`, `RedisLeaderElectionBackend`, `PostgresLeaderElectionBackend`, and `KubernetesLeaderElectionBackend` become `MemoryLeaderElectionAdapter`, `RedisLeaderElectionAdapter`, `PostgresLeaderElectionAdapter`, and `KubernetesLeaderElectionAdapter`. The `LeaderElectionBackend` protocol keeps its name (protocol stays `*Backend`, concrete stays `*Adapter`). Update direct imports and constructions.

### Features

* ✨ Accept a `timedelta` for the interval `seconds=`, like `@task.interval(seconds=timedelta(minutes=2))`. A plain number of seconds still works.
* ✨ Add a `key=` template to `@cached` for a stable, readable cache key rendered from the arguments, like `@cached(key="user:{user_id}")`. Pass `key_maker=` for the fully dynamic case. Passing both raises `TypeError`.
* ✨ Type `FireInfo.outcome` as the new `FireOutcome` `StrEnum` (`SUCCESS`, `ERROR`, `SKIPPED`). String comparisons like `outcome == "success"` still work.
* ✨ Add `Idempotency.run(key, factory)`, a one-call helper that runs an operation once and replays its response. It takes a sync or async factory and mirrors `TTLCache.get_or_set`.
* ✨ Every component now raises a typed `*SettingsValidationError` for invalid configuration, rooted in the shared `SettingsValidationError` base. Adds `TraceSettingsValidationError`, `HealthSettingsValidationError`, `LogSettingsValidationError`, and `IdempotencySettingsValidationError`. Catch `SettingsValidationError` to handle any of them.
* ✨ Cache adapters (`MemoryCacheAdapter`, `RedisCacheAdapter`, `PostgresCacheAdapter`, `SQLiteCacheAdapter`) now declare the `CacheBackend` protocol explicitly, matching the lock, circuit breaker, and rate limiter adapters.
* ✨ Add `Log.from_config`, `Trace.from_config`, and `Metrics.from_config` to build each component from a pre-built config, matching the declarative path on every other pattern. The `config=` kwarg still works.

### Fixed

* 🐛 Name the failing source in the `ExternalConfig` reload warning, so a broken config or secrets mount is no longer a generic warning. Each source loads under its own guard, so a config failure no longer hides a working secrets source. Source values are never logged.

## 1.0.0a2 - 2026-06-21

### Features

* ✨ Add a built-in readiness check per provider. Every connection provider ships a cheap `check()` probe (Redis and Valkey `PING`, Postgres and SQLite `SELECT 1`). Register it with `health.add_provider(redis)` as a critical `provider:redis` check, or register one for every active provider at once with `HealthChecks(auto_health=True)`. `Grelmicro.providers` lists the active providers.

## 1.0.0a1 - 2026-06-12

### Breaking

* 💥 Raise `OutOfContextError` with an actionable message on every ambient backend miss: `Lock`, `TaskLock`, `LeaderElection`, `TTLCache`, `@cached`, the cron schedule resolution, and `Idempotency` now match `CircuitBreaker` and `RateLimiter`. `NoActiveAppError` stays the low-level error raised by `Grelmicro.current()` itself.
* 💥 Remove the implicit memory fallback on `CircuitBreaker` and `RateLimiter`. Backend resolution is now one rule on every pattern: explicit `backend=` wins, else the active app's component, else `OutOfContextError`. For a per-process limiter or breaker without an app, pass `backend=MemoryRateLimiterAdapter()` or `backend=MemoryCircuitBreakerAdapter()` (both import from `grelmicro.resilience`). Inside FastAPI handlers, add `GrelmicroMiddleware` so ambient resolution works there. `RateLimiter.reconfigure` now publishes the config and rebinds the strategy lazily on the next call, matching `CircuitBreaker`.
* 💥 Add positional argument capture to `grelmicro.testing`: `Call` is now `Call(method, args=..., kwargs=...)` and `CallLog.count` matches positional arguments too. Update direct `Call(...)` constructions.
* 💥 Align the leader election and task lock env var prefixes with their single-token names: `GREL_LEADER_ELECTION_{NAME}_` becomes `GREL_LEADERELECTION_{NAME}_` and `GREL_TASK_LOCK_{NAME}_` becomes `GREL_TASKLOCK_{NAME}_`. Update any environment variables set for these components. PR [#346](https://github.com/grelinfo/grelmicro/pull/346).
* 💥 Rename the pattern factory methods so each uses the pattern's single-token name: `Provider.breaker()` becomes `circuitbreaker()`, `leader_election()` becomes `leaderelection()` on both `Provider` and `Coordination`, and `Coordination.task_lock()` becomes `tasklock()`. This matches the module names (`grelmicro.coordination.leaderelection`, `grelmicro.coordination.tasklock`, `grelmicro.resilience.circuitbreaker`) and the `ratelimiter`/`circuitbreaker` kind strings. Update `provider.circuitbreaker()`, `micro.coordination.leaderelection(...)`, and `micro.coordination.tasklock(...)` call sites. PR [#343](https://github.com/grelinfo/grelmicro/pull/343), PR [#344](https://github.com/grelinfo/grelmicro/pull/344).
* 💥 Make `Log`, `Trace`, and `Metrics` singletons. Each configures process-global state (the root logger, the OpenTelemetry tracer and meter providers), so registering a second one on the same app now raises `ComponentAlreadyRegisteredError` instead of silently clobbering the first. PR [#343](https://github.com/grelinfo/grelmicro/pull/343).
* 💥 Make the component `name` a read-only property everywhere (`Coordination`, `Cache`, `Log`, `Trace`, `Metrics`, `RateLimiters`, `CircuitBreakers`, `HealthChecks`, `RealClock`, `VirtualClock`), matching the resilience and coordination primitives. Pass `name=` at construction. PR [#343](https://github.com/grelinfo/grelmicro/pull/343).

### Fixed

* 🐛 Raise an actionable `RuntimeError` from a sync `@cached` call when the backend never captured a running loop, instead of an opaque `AttributeError`. The message says to open the backend with `async with micro:` first.
* 🐛 Reconcile cache tags on every Redis `set` and `set_many`, even with no tags. Re-setting a previously tagged key without tags now drops its stale tag membership, so a later `delete_tags` no longer wrongly removes it. PR [#353](https://github.com/grelinfo/grelmicro/pull/353).
* 🐛 Store the cache sidecar entries (the `early=` refresh metadata, and the new stale reserve) under a `\x1f` separator instead of `\x00`, so they are valid Postgres text keys. `@cached(early=...)` previously raised on a Postgres cache backend. PR [#350](https://github.com/grelinfo/grelmicro/pull/350).

### Features

* ✨ Default the rate limiter `key` to `"default"` on `acquire`, `acquire_or_raise`, `allow`, `peek`, and `reset`, so the single-bucket case is `await limiter.allow()`. The limiter `name` already namespaces the backend key.
* ✨ Add the zero-object `@cached(ttl=30)` form for plain memoization: it binds a private process-local `TTLCache` at decoration, never resolves the active app, and never shares across replicas. Pass a `TTLCache` for shared state. Passing both `cache` and `ttl` raises `TypeError`.
* ✨ Add the OpenSSF Scorecard workflow and badge.
* ✨ Make `Grelmicro(uses=[...])` and `micro.use(...)` forgiving: a bare Component class is constructed for you, a bare adapter (class or instance) is wrapped in its matching Component, and a bare Provider with no Components auto-registers one default Component per kind it serves. The explicit form always wins, so any explicit Component turns provider auto-registration off entirely.
* ✨ Add `AmbiguousProviderError`, raised when `uses=[...]` lists two bare Providers with no Components, so the default Component for a shared kind would be ambiguous. Wrap each Provider in the Components it should serve to resolve it.
* ✨ Add the Idempotency pattern: a new `grelmicro.idempotency` module with an `Idempotency` primitive and `@idempotent` decorator. A caller-provided key (an `Idempotency-Key` header) executes the operation once, stores the response for `ttl` seconds, and replays it on repeats. Duplicates arriving mid-flight fold into the first execution, across replicas when a Coordination lock backend is configured. A failure stores nothing, so a retry executes fresh. An optional `fingerprint=` rejects a reused key with a different payload via `IdempotencyConflictError`. Storage rides the cache layer (`cache=` or the active app's `Cache` component).
* ✨ Add Redis Sentinel and Redis Cluster support: `redis+sentinel://host1:26379,host2:26379/service` and `redis+cluster://host1,host2` URL schemes on `RedisProvider`, plus `RedisProvider.sentinel(...)` and `RedisProvider.cluster(...)` factories, so one URL switches topology. On Cluster, the multi-key cache and lock operations require a hash-tagged prefix (`prefix="{app}cache"`), enforced with a clear error at construction.
* ✨ Add Valkey support: a `ValkeyProvider` in `grelmicro.providers.valkey` (extra `valkey`) serves the full Redis adapter column (Lock, TaskLock, LeaderElection, Schedule, TTLCache, RateLimiter, CircuitBreaker) through the `valkey` client.
* ✨ Add the Externalized Configuration pattern: a new `grelmicro.config` module with an `ExternalConfig` component that reconfigures live components from a mounted ConfigMap, Secret, `.env`, JSON, YAML, or TOML file (`FileConfigAdapter`, nested mappings flatten to env-style keys), polling on an interval with a public `reload()` for an immediate pass. Sources are pluggable via the `ConfigBackend` protocol. Every named pattern built imperatively registers under its `GREL_{PATTERN}_{NAME}_` keys, including `CircuitBreaker` (`GREL_CIRCUITBREAKER_{NAME}_`) and `RateLimiter` (`GREL_RATELIMITER_{NAME}_`). Instances built from a pre-built config stay static. Validation warnings log field names only, never values.
* ✨ Add `GrelmicroMiddleware` in `grelmicro.fastapi`: a pure ASGI middleware that binds the active app inside request handlers, so `Lock("cart")`, `RateLimiter.sliding_window(...)`, and `@cached` resolve ambiently in handlers without explicit `backend=` wiring.
* ✨ Add a bounded wait to `Lock.acquire(timeout=)`, raising `TimeoutError` at the deadline, and `Lock.extend()` to renew the lease of a held lock without releasing it. Both are mirrored on the `from_thread` facade.
* ✨ Add `TaskLock.refresh()` so a task body that may outrun `max_lock_seconds` can renew its claim, raising `LockNotOwnedError` when the claim was lost.
* ✨ Add `retry_jitter` to `Lock` and `LeaderElection` (default 0.1): each retry sleeps `retry_interval * uniform(1 - jitter, 1 + jitter)`, so contending workers spread their attempts instead of retrying in lockstep.
* ✨ Add scheduler introspection: `next_fire_time` and `last_fire` on interval and cron tasks, with `FireInfo` (started_at, outcome, duration) exported from `grelmicro.task`.
* ✨ Add `Match.explain()` returning the human-readable matcher tree, and warn once when a predicate returns a non-bool value.
* ✨ Add a shared `AdmissionError` base so every gatekeeping rejection is catchable with one `except`. `RateLimitExceededError`, `BulkheadFullError`, `CircuitBreakerError`, and `WouldBlockError` now inherit it, so `except AdmissionError` handles a rate limiter over budget, a full bulkhead, an open circuit breaker, or a non-blocking lock that would block. It is purely additive: the existing per-primitive `except` clauses still work. PR [#354](https://github.com/grelinfo/grelmicro/pull/354).
* ✨ Add `RateLimiter.allow(key=...)` returning a `bool` for the common served-or-throttled branch, and make `RateLimitResult` truthy (`bool(result)` is `result.allowed`). `if await limiter.acquire(key=...):` now reads as the decision while `retry_after` and `remaining` stay available on the result. PR [#354](https://github.com/grelinfo/grelmicro/pull/354).
* ✨ Add serve-stale-on-error to the cache with `stale_ttl=` on `@cached`, `get_or_set`, and `TTLCache.set`. Each value keeps a fallback copy for `ttl + stale_ttl` seconds, so a recompute that fails after the TTL serves the last good value instead of raising, up to `stale_ttl` seconds late. A flaky upstream degrades to slightly stale data instead of an error storm. It composes with `lock` and `early`, an explicit delete or tag invalidation drops the fallback, and each stale serve records the `grelmicro.cache.stale_serves` metric. PR [#350](https://github.com/grelinfo/grelmicro/pull/350).
* ✨ Add SQLite cache and circuit breaker backends, completing the SQLite column of the capability matrix (the circuit breaker coordinates single-host multi-process state). PR [#349](https://github.com/grelinfo/grelmicro/pull/349).
* ✨ Add a durable `@tasks.cron(expr, timezone="UTC")` decorator that runs a task on a 5-field cron schedule (`minute hour day-of-month month day-of-week`). The parser is built in, with no external dependency, and supports `*`, steps, ranges, lists, and the `7`-as-Sunday alias. It uses standard Vixie day-of-month/day-of-week OR semantics. Each fire is claimed against a durable last-fire state via a new `ScheduleBackend` (Memory, Redis, Postgres, and SQLite), so the task runs at most once across all workers per fire. A fire missed while every worker was down replays once on restart, bounded by `misfire_grace_seconds`, and only the most recent missed fire runs. Wire it via `Coordination(provider)` or `Coordination(schedule=...)`. PR [#348](https://github.com/grelinfo/grelmicro/pull/348).
* ✨ Add a time-based stop to `Retry` with `max_seconds=`. Retrying stops as soon as either `attempts` is reached or the wall-clock budget elapses, whichever comes first (`attempts` still defaults to 3). Available on the `Retry.exponential`/`Retry.constant` factories, the constructor, and `RetryConfig` (env var `GREL_RETRY_{NAME}_MAX_SECONDS`). The budget reads the clock seam, so `VirtualClock` drives it in tests. PR [#347](https://github.com/grelinfo/grelmicro/pull/347).
* ✨ Re-export `FunctionTypeError` and `TaskAddOperationError` from `grelmicro.task`, so the task errors users catch live next to `TaskError` instead of only in `grelmicro.task.errors`. PR [#343](https://github.com/grelinfo/grelmicro/pull/343).
* ✨ Export the catch-all base `GrelmicroError` and the cross-cutting `DependencyNotFoundError`, `OutOfContextError`, and `SettingsValidationError` from the top-level `grelmicro` package, so `except GrelmicroError` catches any library error from one import. Re-export `WouldBlockError` and `CoordinationBackendError` from `grelmicro.coordination` (the latter moved into `grelmicro.coordination.errors`). PR [#343](https://github.com/grelinfo/grelmicro/pull/343).

### Docs

* 📝 Add `docs/architecture/decorators.md` documenting the bare `@deco` versus parametrized `@deco(...)` rule and which decorators wrap sync functions. PR [#343](https://github.com/grelinfo/grelmicro/pull/343).

## 0.27.0 - 2026-06-07

### Breaking

* 💥 Replace `@cached(stampede="local" | "distributed" | None)` with `@cached(lock=False | True | "local")`. `lock=True` folds concurrent misses and picks the cross-replica path automatically when the active app has a `Coordination` lock backend (in-process otherwise), `lock="local"` forces the in-process path, and the default is now `lock=False` (no protection, opt in explicitly). Migrate `stampede="local"` to `lock="local"`, `stampede="distributed"` to `lock=True`, and `stampede=None` to `lock=False`. Issue [#235](https://github.com/grelinfo/grelmicro/issues/235).
* 💥 Move `LeaderElection` out of `grelmicro.sync` into a new `grelmicro.coordination` package. Import it from `grelmicro.coordination`. `Sync.leader_election()` is removed: register a `Coordination` component and call `micro.coordination.leader_election(...)`. Leader election now runs on a dedicated `LeaderElectionBackend`, not the lock `SyncBackend`, so it can use a different vendor than `Lock` (Redis for `Lock`, a Kubernetes Lease for leader election). Issue [#223](https://github.com/grelinfo/grelmicro/issues/223).
* 💥 Unify `grelmicro.sync` into `grelmicro.coordination` and delete `grelmicro.sync`. Import `Lock`, `TaskLock`, `LeaderElection`, and `Coordination` from `grelmicro.coordination`. The `Sync` component is gone: use one `Coordination` component, which exposes `.lock(...)`, `.task_lock(...)`, and `.leader_election(...)`, and reach it on `micro.coordination`. The `SyncBackend` protocol is now `LockBackend`, the `*SyncAdapter` backends are now `*LockAdapter`, and the provider factory `.sync()` is renamed to `.lock()`. Issue [#223](https://github.com/grelinfo/grelmicro/issues/223).
* 💥 Make the JSON utilities internal. The `grelmicro.json` module is removed. Use `JsonSerializer` from `grelmicro.cache` for cache JSON, or `orjson` directly if you need raw fast JSON.

### Features

* ✨ Add a `Metrics` component that installs an OpenTelemetry `MeterProvider` for the app's lifetime, with OTLP, Prometheus, console, and none exporters. A `@measure` decorator times and counts any function, `metrics_router()` serves a Prometheus `/metrics` endpoint, and every built-in component (health, circuit breaker, retry, rate limiter, bulkhead, timeout, cache, tasks) emits its own metrics. All metric calls are no-ops without the `opentelemetry` extra or an active component.
* ✨ Leader election leases carry a Kubernetes-style `LeaderRecord` (holder, lease duration, acquire and renew times, leadership transitions, and free-form metadata). Read it from `LeaderElection.record`, set the metadata via `LeaderElection(metadata=...)`. Metadata-storing backends ship for memory, Redis, Postgres, and Kubernetes Lease, resolved through `provider.leader_election()` or passed to `Coordination(...)` directly. Issue [#223](https://github.com/grelinfo/grelmicro/issues/223).
* ✨ Add `grelmicro.testing.record(backend)` for protocol-level call assertions. It instruments a backend's public async methods in place and returns a `CallLog`, so the backend keeps its type and behavior while every call is recorded. Assert with `log.count(method, **kwargs)`, inspect `log.methods()`, or read the raw `log.calls`. Works like `pytest-mock`'s `mocker.spy`. Issue [#271](https://github.com/grelinfo/grelmicro/issues/271).
* ✨ Add cache tags, `get_or_set`, and batch operations. Tag entries via `set`, `set_many`, `get_or_set`, or `@cached(tags=["users", "user:{user_id}"])`, then invalidate a whole group with `delete_tags`. `get_or_set(key, factory)` computes a missing value once under the same stampede protection as `@cached(lock=True)`. `get_many`, `set_many`, and `delete_many` work on many keys at once. Tags and batch ops run on Memory, Redis, and Postgres.
* 📝 Correct the comparison page and capability matrix to show the Postgres and SQLite cache, rate limiter, and circuit breaker backends as shipped (they were stale-labeled "planned").
* 📝 Add a "what grelmicro is not" line to the README and docs landing for sharper first-read positioning.
* 🔧 Set the PyPI `Development Status` classifier to `4 - Beta`.
* ✨ Discover Providers and Adapters through entry-point groups. Third-party packages register under `grelmicro.providers` and `grelmicro.{kind}.adapters` (`coordination`, `cache`, `ratelimiter`, `circuitbreaker`) and resolve by short name, the same path first-party backends use. Unknown names raise `ProviderNotRegisteredError` or `AdapterNotRegisteredError` listing the installed names. New `docs/architecture/plugins.md` and an `examples/third-party-adapter/` skeleton. Issue [#234](https://github.com/grelinfo/grelmicro/issues/234).
* ✨ Add `VirtualClock` for deterministic time in tests. Time-dependent primitives (`Retry` backoff, `CircuitBreaker` half-open window, `RateLimiter` refill, `Shield` adaptive gate) read time through a clock seam (`grelmicro.clock.monotonic` / `sleep`). Install a `VirtualClock` (`Grelmicro(uses=[clock, ...])` or `async with VirtualClock()`) and call `clock.advance(seconds)` to drive that behavior with no real waiting. With no clock registered, the seam forwards to `time.monotonic` and `asyncio.sleep`, so production keeps real time. Issue [#272](https://github.com/grelinfo/grelmicro/issues/272).
* ✨ Auto-discover shared Providers in `Grelmicro(uses=[...])`. A Provider held by a Component (`Coordination(redis)`, `Cache(redis)`) no longer has to be listed separately: it is adopted and lifecycled exactly once, opened before the Components that hold it. Listing it explicitly stays valid and keeps control over lifecycle order. Issue [#263](https://github.com/grelinfo/grelmicro/issues/263).

### Docs

* 📝 Lead every feature page with the simplest runnable example, then explain, moving deep theory into collapsible sections. Covers the resilience patterns and the cache, coordination, logging, health, tracing, and task guides.

## 0.26.0 - 2026-06-05

### Breaking

* 💥 The `Task` protocol's `__call__` now takes a `stop: asyncio.Event | None = None` keyword used for graceful shutdown. Custom `Task` implementations must accept it. The built-in `interval` tasks and `LeaderElection` are unaffected. Issue [#187](https://github.com/grelinfo/grelmicro/issues/187).
* 💥 Replace the `@cached(lock=...)` parameter with `@cached(stampede="local" | "distributed" | None)`. `lock=True` becomes `stampede="local"` (now the default), `lock=False` becomes `stampede=None`, and the custom-context-manager form is dropped in favor of the `"distributed"` cross-replica mode. Issue [#235](https://github.com/grelinfo/grelmicro/issues/235).

### Features

* 📝 Add a runnable FastAPI demo under `examples/fastapi-demo/`. `docker compose up --wait` starts Redis, Postgres, and a FastAPI app that exercises every Pattern (cache, rate limiter, circuit breaker, distributed lock, leader-gated task, health probes), with a `Demo Smoke` CI job and a `just demo` shortcut. Issue [#166](https://github.com/grelinfo/grelmicro/issues/166).
* 📝 Add a ConfigMap-watcher example wiring `reconfigure()` (`docs/configuration/reconfigure-from-configmap.md`), with a `SIGHUP` variant for non-Kubernetes hosts. Issue [#169](https://github.com/grelinfo/grelmicro/issues/169).
* ✨ Accept bare zero-arg classes in `Grelmicro(uses=[...])`, `micro.use(...)`, and the `Sync` / `Cache` / `RateLimiters` / `CircuitBreakers` constructors. `uses=[MemorySyncAdapter]` and `Sync(MemorySyncAdapter)` now work without the trailing `()`, in the spirit of FastAPI's `Depends(dep)`. A class that needs constructor arguments raises a clear error. Issue [#263](https://github.com/grelinfo/grelmicro/issues/263).
* ✨ Guard against two overlapping `Grelmicro` apps clobbering process-global state. Opening a second app that registers `Log` or `Trace` while another such app is active now raises `MultipleActiveAppsError`. Apps without those components overlap freely. Pass `Grelmicro(allow_multiple=True)` to opt out. New `docs/architecture/multiple-apps.md` documents the policy. Issue [#266](https://github.com/grelinfo/grelmicro/issues/266).
* ✨ Add `Tasks(shutdown_timeout=...)` for graceful shutdown. On exit, `Tasks` signals every `interval` task to finish its current run and stop, force-cancelling only stragglers that outlast the timeout. The default `30.0` matches Kubernetes' `terminationGracePeriodSeconds`, and `LeaderElection` releases leadership on the same signal. New `docs/architecture/graceful-shutdown.md` covers signal wiring. Issue [#187](https://github.com/grelinfo/grelmicro/issues/187).
* ✨ Add a three-layer cache stampede menu to `@cached`. `stampede="local"` (default) folds concurrent same-key misses to one in-process run, `stampede="distributed"` coordinates across replicas through the `Sync` component, and `early=` (XFetch) refreshes the hottest keys in the background before they expire so no caller blocks. Issue [#235](https://github.com/grelinfo/grelmicro/issues/235).
* ✨ Add `LeaderElection.last_confirmation_age()` (seconds since the last backend response that confirmed local leadership, `None` until first acquisition and after confirmed loss) and `LeaderElection.is_leader_confirmed_within(max_age)` (stricter variant of `is_leader()` that requires a recent backend renewal). The `is_leader()` docstring now spells out the advisory uncertainty window during a backend partition.
* ✨ Add `Grelmicro(strict=True)` to raise `LifecycleOrderError` instead of warning when a Component holds a Provider that is missing from `uses=` or listed after the dependent Component. The default `False` preserves the lenient warn-only behavior. `LifecycleOrderError` is exported from `grelmicro`.
* ✨ Add `Shield` resilience pattern: per-attempt timeout, retry-budget-gated retries, CUBIC-style adaptive rate limiter, optional cache and fallback recovery paths. Three profiles (`internal`, `api`, `slow`) cover the common cases. Decorator (`@shield`, `@shield.api(...)`), class (`Shield.api("name")`), and imperative (`Shield.api("name").run(fn, ...)`) forms supported. Issue [#249](https://github.com/grelinfo/grelmicro/issues/249).
* ✨ Add `TTLCacheConfig` and expose it via `TTLCache.config`. Matches the frozen-config shape used by every other primitive.
* ✨ Add `RedisProvider.safe_url` and `PostgresProvider.safe_url` returning the resolved URL with the password replaced by `***`. The new `__repr__` on both providers uses the safe form so credentials never leak through logs or tracebacks.
* ✨ Add `TracingConfig.shutdown_timeout` (default `5.0` seconds). `Trace.__aexit__` now runs `TracerProvider.shutdown()` in a thread with this deadline so a slow or broken exporter no longer hangs application shutdown.
* ✨ Add `SQLiteProvider` and SQLite rate limiting. Use `RateLimiters(SQLiteProvider("app.db"))` for file-backed limits on a single host. Each acquire runs a read-modify-write inside a `BEGIN IMMEDIATE` transaction. Issue [#173](https://github.com/grelinfo/grelmicro/issues/173).
* ✨ Add `PostgresCircuitBreakerAdapter` for fleet-wide circuit breaker state on Postgres, plus `PostgresProvider.breaker()` so `CircuitBreakers(postgres)` resolves it. Transitions run in PL/pgSQL functions guarded by `pg_advisory_xact_lock`.
* ✨ Add `Bulkhead` resilience pattern to cap concurrent in-flight calls. `max_concurrent` bounds concurrency, `max_wait` lets callers queue briefly before a `BulkheadFullError` (default fails fast), and `max_workers` runs blocking work through `bulkhead.to_thread` on a dedicated pool. Async context manager and decorator forms. Issue [#168](https://github.com/grelinfo/grelmicro/issues/168).
* ✨ Add `Bulkhead(uses=[...])` to scope Providers and Components to a bulkhead. Inside the scope, a Pattern resolving its default backend picks up the bulkhead's Component, isolating a business context onto its own pool. Explicit `backend=` still wins. Issue [#168](https://github.com/grelinfo/grelmicro/issues/168).

### Fixes

* 🐛 The README and `simple_fastapi_app.py` FastAPI examples now pass an explicit `backend=` to patterns used inside request handlers. Request handlers run outside the app's ambient `Grelmicro.current()` scope, so the previous ambient form raised `NoActiveAppError` (locks, cache) or silently fell back to an in-memory backend (rate limiter, circuit breaker) at runtime. Background `Tasks` keep using ambient resolution. Ambient resolution in handlers is tracked in [#328](https://github.com/grelinfo/grelmicro/issues/328).
* 🔒 `SettingsValidationError` no longer echoes the offending input value. Env-loaded credentials (DSNs, tokens) no longer surface in error messages.
* 🚨 `ComponentNotRegisteredError` from `Grelmicro.get(kind, name)` now lists every registered `(kind, name)` pair (or states that none are registered). Agents and developers see what is available without inspecting the container.
* 🚨 `HealthChecks.add` invalid-name errors now include valid examples (`'redis'`, `'db-primary'`, `'weather:circuitbreaker'`) alongside the regex.
* 🐛 `Log.__aenter__` and `Log.__aexit__` now serialize on a class-level `threading.Lock` so concurrent `Grelmicro` lifecycles in the same process cannot interleave the stdlib root-logger snapshot / restore sequence.
* 🐛 Unexpected exceptions inside a health check now surface as `"TypeName: message"` in the `CheckResult.error` field instead of the generic `"Health check failed"`. Operators reading only the `/healthz` payload can identify the failing class without grepping logs.
* 🔒 `Lock("...")` now validates the name against `^[A-Za-z0-9][A-Za-z0-9._:/-]*$` (max 200 chars). Names with whitespace, control characters, or leading separators are rejected with a message that includes valid examples. Existing namespaced names (`users:42`, `payments/eu`, `weather.svc`) keep working.
* ⚡ `DuplicateFilter` now sweeps entries older than `ttl_seconds` once per window, so high-cardinality log floods stop evicting still-active keys by size pressure.

### Docs

* 📝 Lead `README.md` and `docs/index.md` with a one-route, one-primitive FastAPI example before the full composition demo.
* 📝 Annotate `Grelmicro.use`, `Grelmicro.get`, `instrument`, and `CacheBackend` protocol parameters with `Annotated[..., Doc(...)]`.
* 📝 Align the `CONTRIBUTING.md` discriminator rule with the code: `kind` (not `type`).
* 📝 Document the per-process scope of `Tasks` and point at `TaskLock` / `LeaderElection` for cluster-wide scheduling.
* 📝 Add a Kubernetes operational-assumptions section covering RBAC, API server availability, etcd latency, and single-cluster scope to `docs/architecture/kubernetes.md`.
* 📝 Fix the `sync.md → task.md#tasks` internal anchor so `mkdocs --strict` no longer reports it.
* 📝 Add a lifespan-only example (one provider, one component) between the minimal example and the full composition demo in `README.md` and `docs/index.md`.
* 📝 Drop unsupported claims and idioms from the landing copy ("Stop reinventing the wheel", "battle-tested in production", "TL;DR").
* 📝 Add `Start here` / `Common recipes` lead lines to every page under `docs/reference/`.
* 📝 Add an explicit `Running tests` section to `CONTRIBUTING.md` with the commands for unit-only, integration-only, and the full local gate.
* 📝 Add a `What should I pick?` decision tree to the top of `docs/comparison.md` so readers can map their situation to the right tool (one primitive, two or more, task queue, workflow engine, web framework).
* 📝 Add a `Your first contribution` section to `CONTRIBUTING.md` with the expected code, test, and docs shape and a pointer to the `good first issue` label.
* 📝 Add `Annotated[..., Doc(...)]` to the `SyncBackend`, `RetryStrategy`, `RateLimiterStrategy`, `RateLimiterBackend`, `CircuitBreakerStrategy`, and `CircuitBreakerBackend` protocol parameters so IDE and LLM tools surface the same hints on backends as on user-facing primitives.
* 📝 Group the `grelmicro.resilience` package docstring into front doors, components, adapters, and configs so import-site hover help guides agents and humans to the preferred entry point.
* 📝 Document that auto-generated task references (`module:qualname`) surface in logs, distributed lock keys, and metric labels. Suggest passing an explicit `name=` for sensitive workflows in `validate_and_generate_reference` and the `docs/task.md` Interval Task section.
* 📝 Add a `Why Python 3.12` section to `docs/installation.md` listing the language features (PEP 695, `asyncio.timeout`) that drive the floor, and note that CI runs the matrix on every advertised classifier (3.12, 3.13, 3.14).
* 📝 Add a `Platforms` column to the Optional extras table in `docs/installation.md` calling out that `uvloop` is skipped on Windows and PyPy.
* 📝 Document `RateLimitResult.remaining` as an estimate for continuous-state algorithms (GCRA-based sliding window). Enforcement still uses exact state, so the next `acquire` may be denied even when `remaining > 0`.
* 📝 Add a FastStream resilience recipe (`docs/snippets/resilience/faststream.py`) that uses a fleet-wide per-key `Lock` and a sliding-window `RateLimiter` inside a Redis-broker subscriber. Linked from `docs/resilience/index.md`.
* 📝 Formalize the `test_<component>_<scenario>_<expected_outcome>` test-name shape in `CONTRIBUTING.md` with three concrete examples.
* 📝 Add `docs/benchmarks.md` with reproducible request-path benchmarks for the rate limiter, circuit breaker, cache, and lock, plus runnable scripts under `benchmarks/`.
* 📝 Add a `Choosing a backend` guide to the sync, cache, rate limiter, and circuit breaker pages.
* 📝 Expand `docs/json.md` with supported types, the orjson fallback, and serializer boundaries.
* 📝 Note that the default OTLP HTTP trace exporter expects a running collector in `docs/tracing.md`, with `CONSOLE` and `NONE` for local development.

### Internal

* 🔒 `@instrument` now filters arguments whose names match common secret keywords (`password`, `token`, `secret`, `api_key`, `authorization`, `cookie`, ... matched case-insensitively) from both span attributes and log context. Pass extra names via `skip=` for custom secret-bearing parameters. Unchanged for non-sensitive args.
* 🔧 Replace the optional `orjson` redef-as-`Any | None` pattern in `grelmicro/_json.py` with try/except branches that define the dumps/loads functions in scope. The per-call `# type: ignore[union-attr]` directives are gone, and `orjson` keeps its real type from the stub package in the available branch.
* 🚨 `Trace.__aenter__` now raises `TracingError` if `opentelemetry.trace._TRACER_PROVIDER` is missing instead of silently no-op patching. A future OTel that drops the private global surfaces a clear error pointing at the workaround. An inline comment near the patch documents why the private attribute is required.
* 🔒 `PickleSerializer` docs upgraded to a Danger callout. Pickle is now framed as trusted in-process backends only, and the `@cached` decorator example leads with `JsonSerializer`. The `TTLCache` docstring lists Pydantic and JSON before Pickle.
* 🔧 Comment why `_env_prefix=env_prefix` needs a type-ignore in `RedisProvider` and `PostgresProvider` (pydantic-settings runtime kwarg the stubs do not expose).
* ⚡ Snapshot hot config fields (`cost`, `allowed_repetitions`, `ttl_seconds`, `cache_size`) onto `RateLimitFilter` and `DuplicateFilter` instances during setup so the per-record `filter()` path reads plain attrs instead of walking the Pydantic config.
* 🔧 Drop three unused `ty: ignore` directives in `grelmicro/_json.py`.
* ⚡ `@cached(lock=True)` per-key lock dictionaries now bound their size with LRU eviction (1024 entries). High-cardinality miss-heavy workloads no longer accumulate `asyncio.Lock` / `threading.Lock` objects indefinitely. Held locks are never evicted, so in-flight stampede protection is preserved.
* 🔒 `PostgresRateLimiterAdapter` advisory locks now use `pg_advisory_xact_lock(hashtextextended(key, namespace))`. The grelmicro-specific seed gives rate-limiter keys their own 64-bit lock-id space, isolating them from any other advisory lock in the same database and reducing intra-rate-limiter collisions from a 32-bit birthday risk to a 64-bit one.
* ✅ Add a `tests/typing/` sample (`test_cache_generics.py`) that uses `typing.assert_type` to lock in `TTLCache[T]`, `PickleSerializer[T]`, and `PydanticSerializer[T]` inference end-to-end. A regression that widens inference back to `Any` fails `uv run ty check`.
* ✅ Add a guard test that every `_LAZY` key in `grelmicro/resilience/__init__.py` is exported in `__all__` and actually resolves at runtime.
* ✅ Add Hypothesis property tests for token-bucket and sliding-window math and for exponential backoff jitter bounds.
* ✅ Enable branch coverage (`--cov-branch`). The 100% gate now covers both lines and branches. Defensive guards against impossible state are marked with `# pragma: no branch`.
* 🔧 Document why every `type: ignore` and `ty: ignore` in `grelmicro/_config.py` is required (Pydantic dynamic-subclass boundary).
* 🔧 Explain the double-checked `pragma: no cover` in `Reconfigurable.reconfigure` so future contributors see the concurrent-caller intent.
* 🔧 Add inline attribution cues to `grelmicro/task/_utils.py` and `grelmicro/resilience/_protocol.py` / `grelmicro/cache/_protocol.py` so readers immediately see where third-party adaptations live and that protocol bodies live in concrete adapters.
* 🔧 Fix the `THIRD_PARTY_NOTICES.md` path to `grelmicro/resilience/ratelimiter/redis.py`.

## 0.25.0 - 2026-05-21

### Features

* ✨ Add `Timeout` reconfigurable resilience pattern. `Timeout("db", seconds=2.0)` wraps `asyncio.timeout`, usable as an async context manager (`async with db_timeout:`) or decorator on async functions. `TimeoutConfig` is a frozen three-paths Pydantic config with `seconds: PositiveFloat`. Env prefix `GREL_TIMEOUT_{NAME_UPPER}_`. Inherits `Reconfigurable[TimeoutConfig]` for live deadline swaps. Issue [#176](https://github.com/grelinfo/grelmicro/issues/176).
* ✨ Add `Fallback` primitive with decorator, block, and class forms. `@fallback(when=..., default=...)` / `@fallback(when=..., factory=...)` swap a matched exception for a safe value. `async with falling_back(when=..., default=...) as result:` covers inline blocks. `Fallback("name", when=..., default=...)` is the named, reconfigurable class form. `FallbackConfig` is a frozen three-paths Pydantic config with `default` / `factory` mutually exclusive. `when=` matches Retry's keyword so the `Match` DSL stays universal. Composition order documented in [Composing patterns](resilience/composition.md). Issue [#199](https://github.com/grelinfo/grelmicro/issues/199).
* ✨ Add `PostgresCacheAdapter` for Postgres-backed cache storage. Register via `Grelmicro(uses=[postgres, Cache(postgres)])`. Entries land in a single `grelmicro_cache` table keyed on `key TEXT PRIMARY KEY` with `value BYTEA` and `expires_at TIMESTAMPTZ`. `get` filters expired rows with `WHERE expires_at > NOW()`, `set` is one `INSERT ... ON CONFLICT DO UPDATE`. Schema auto-migrates on first connect, opt out with `auto_migrate=False`. Optional janitor reclaims storage when `cleanup_interval=` is set (off by default). Issue [#167](https://github.com/grelinfo/grelmicro/issues/167).
* ✨ Add `PostgresRateLimiterAdapter` for fleet-wide rate limiting on Postgres. Register via `Grelmicro(uses=[postgres, RateLimiters(postgres)])` and `RateLimiter.token_bucket(...)` or `RateLimiter.sliding_window(...)` runs against a single `grelmicro_rate_limiter` table. `acquire` and `peek` each run one round-trip to a PL/pgSQL function. Concurrent writes for the same key are serialized with `pg_advisory_xact_lock`. Schema and functions auto-migrate on first connect, opt out with `auto_migrate=False`. Issue [#164](https://github.com/grelinfo/grelmicro/issues/164).

## 0.24.0 - 2026-05-18

### Features

* ✨ Add `CircuitBreakerStrategy` Protocol and `CircuitBreakerBackend.bind(name, config) -> Strategy`. Mirrors the RateLimiter shape so a second algorithm plugs in without breaking changes. `CircuitBreakerConfig` gains a `kind: Literal["consecutive_count"]` discriminator. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* ✨ Add `RedisCircuitBreakerAdapter` for fleet-wide breaker state. Register via `Grelmicro(uses=[redis, CircuitBreakers(redis)])` and `CircuitBreaker("name")` consults Redis for admission, counters, and transitions. Half-open admission cap is enforced globally via atomic Lua scripts. `last_error` and `last_error_time` stay per-replica. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* ✨ Add `CircuitBreaker.consecutive_count(name, ...)` factory classmethod, mirroring `RateLimiter.token_bucket(...)` and `RateLimiter.sliding_window(...)`. Each algorithm of every Pattern lands as a classmethod on the Pattern class. The algorithm-config module loads lazily on first call. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* ✨ `grelmicro.resilience` is now a PEP 562 lazy package: `from grelmicro.resilience import CircuitBreaker` no longer loads `RateLimiter`, its algorithm configs, or memory/redis adapters. Same in the other direction. Top-level `__getattr__` dispatches to the right subpackage on first access. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).

### Breaking

* 💥 `CircuitBreaker.transition_to_closed`, `transition_to_open`, `transition_to_half_open`, `transition_to_forced_open`, `transition_to_forced_closed`, and `restart` are now `async def`. Add `await` at every call site. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 `CircuitBreakerBackend` Protocol is now lifecycle + `bind(name, config)`. Custom backends should return a `CircuitBreakerStrategy` instance from `bind`. `register(breaker)` and the local fast-path are dropped: every backend (including `MemoryCircuitBreakerAdapter`) goes through the Strategy. Memory state lives in adapter-owned dicts keyed by breaker name. `CircuitBreakerSharedState` renamed to `CircuitBreakerSnapshot`. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 `CircuitBreakerConfig` is now a `Discriminator("kind")`-tagged union (matches `RateLimiterConfig`). Instantiate `ConsecutiveCountConfig(...)` directly. The algorithm config lives at `grelmicro.resilience.circuitbreaker.consecutive_count`. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 `FORCED_OPEN` and `FORCED_CLOSED` no longer increment `consecutive_error_count` / `consecutive_success_count`. Per-replica `total_error_count` / `total_success_count` still tick. Dashboards keying off consecutive counts during forced states need updating. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 Resilience layout: each Pattern is now a subpackage with its algorithm configs and adapters as siblings. `grelmicro.resilience.memory` → `grelmicro.resilience.circuitbreaker.memory` and `grelmicro.resilience.ratelimiter.memory`. `grelmicro.resilience.redis` → `grelmicro.resilience.circuitbreaker.redis` and `grelmicro.resilience.ratelimiter.redis`. `grelmicro.resilience.algorithms` is gone: rate-limiter configs live at `grelmicro.resilience.ratelimiter.{token_bucket,sliding_window}`, circuit-breaker configs at `grelmicro.resilience.circuitbreaker.consecutive_count`. Top-level `from grelmicro.resilience import ...` shortcuts are unchanged. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 Rename Components: `Breaker` → `CircuitBreakers`, `RateLimit` → `RateLimiters`. Plural matches existing Component convention (`Tasks`, `HealthChecks`). Mechanical migration: replace `Breaker(...)` with `CircuitBreakers(...)` and `RateLimit(...)` with `RateLimiters(...)` at every call site. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 `CircuitBreaker.__init__` drops the algorithm kwargs path (`error_threshold=`, `success_threshold=`, `reset_timeout=`, `half_open_capacity=`, `log_level=`, `ignore_exceptions=`, `env_prefix=`, `env_load=`). Signature is now `CircuitBreaker(name, config=None, *, backend=None)`, matching `RateLimiter`. Use `CircuitBreaker.consecutive_count("name", error_threshold=5, ...)` for the simple case, `CircuitBreaker("name", ConsecutiveCountConfig(...))` for the declarative case, or bare `CircuitBreaker("name")` for defaults. Env loading via `GREL_CIRCUIT_BREAKER_*` is gone: build the config from `pydantic-settings` if you need that. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* ✨ `CircuitBreaker` and `RateLimiter` fall back to a process-global implicit `MemoryCircuitBreakerAdapter` / `MemoryRateLimiterAdapter` when no `CircuitBreakers` / `RateLimiters` Component is registered. `CircuitBreaker("payments")` and `RateLimiter.token_bucket("api", capacity=10, refill_rate=1)` work without any `Grelmicro(uses=[...])` setup. Fleet-wide opt-in stays explicit (`Grelmicro(uses=[redis, CircuitBreakers(redis), RateLimiters(redis)])`). Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).

## 0.23.0 - 2026-05-17

### Breaking

* 💥 Rename the discriminator field from `type` to `kind` on every tagged union. Affects `RateLimiterConfig` (`TokenBucketConfig`, `SlidingWindowConfig`) and `RetryBackoffConfig` (`ExponentialBackoff`, `ConstantBackoff`, `LinearBackoff`, `FibonacciBackoff`, `RandomBackoff`). Serialized YAML and JSON configs must replace `type:` with `kind:` (for example `GREL_RETRY_FOO_BACKOFF={"kind":"exponential",...}`). Frees the Python `type` builtin from being shadowed on every config object. Issue [#268](https://github.com/grelinfo/grelmicro/issues/268).
* 💥 Rename `GCRAConfig` to `SlidingWindowConfig` and `RateLimiter.gcra(...)` to `RateLimiter.sliding_window(...)`. The discriminator value also moves from `"gcra"` to `"sliding_window"`. Module `grelmicro.resilience.algorithms.gcra` becomes `grelmicro.resilience.algorithms.sliding_window`. Internal strategy classes (`_RedisGCRA`, `_MemoryGCRA`) keep their names since they describe the underlying algorithm. Issue [#259](https://github.com/grelinfo/grelmicro/issues/259).

### Features

* ✨ Add `Log` and `Trace` components. Register `Log()` and `Trace()` in `Grelmicro(uses=[...])` to wire observability through the same verb as `Sync`, `Cache`, `RateLimit`, `Breaker`, and `Tasks`. `Log()` wraps `grelmicro.log.configure(...)` and snapshots stdlib root handlers on enter so sequential apps in tests do not stack handlers. `Trace()` owns an OTel `TracerProvider`: builds it from `TracingConfig` (env prefix `GREL_TRACE_`), installs it on enter, shuts it down and restores the prior global provider on exit. OTLP HTTP and gRPC exporters are lazy-imported. Issue [#224](https://github.com/grelinfo/grelmicro/issues/224).

### Docs

* 📝 Add [Testing](architecture/testing.md) page documenting `micro.override(...)` and the conftest recipe. Issue [#236](https://github.com/grelinfo/grelmicro/issues/236).
* 📝 Add [Capability matrix](capabilities.md) page covering Pattern × Adapter pairs for `1.0.0`. Issue [#161](https://github.com/grelinfo/grelmicro/issues/161).

## 0.22.0 - 2026-05-16

### Features

* ✨ Add `Grelmicro` app object and `Component` protocol. The user composes everything attached to the app into one container and opens it with `async with micro:`. Single `Grelmicro.use(item)` registration verb (and `uses=` constructor kwarg) accepts `Component` instances (registered with `(kind, name)` lookup, exposed on `micro.<kind>`), first-party backends (auto-wrapped into their matching Component: `RedisCacheAdapter` → `Cache`, `RedisSyncAdapter` → `Sync`), and any other async context manager (lifecycled only, caller keeps the reference). Typed accessors `micro.sync` and `micro.cache` provide IDE completion. `Grelmicro.components` returns the registered Components in order for `/healthz`-style introspection. Issue [#208](https://github.com/grelinfo/grelmicro/issues/208), epic [#201](https://github.com/grelinfo/grelmicro/issues/201), unified in [#219](https://github.com/grelinfo/grelmicro/issues/219), `Component` rename and `.components` accessor in [#233](https://github.com/grelinfo/grelmicro/issues/233).
* ✨ Add `Sync` component. Wraps a `SyncBackend` and exposes `lock(...)`, `task_lock(...)`, `leader_election(...)` factories. Use it via `Grelmicro(uses=[redis, Sync(redis)])` (Provider-direct) or `Sync(MemorySyncAdapter())` (Backend-direct). Reach it on `micro.sync`. Issue [#210](https://github.com/grelinfo/grelmicro/issues/210).
* ✨ Add `Cache` component. Wraps a `CacheBackend` and exposes a `ttl(...)` factory that builds a `TTLCache` bound to the wrapped backend. Use it via `Grelmicro(uses=[redis, Cache(redis)])` (Provider-direct) or `Cache(MemoryCacheAdapter())` (Backend-direct). Reach it on `micro.cache`. Issue [#212](https://github.com/grelinfo/grelmicro/issues/212).
* ✨ Add Component-direct Provider API. `Sync`, `Cache`, `RateLimit`, and `Breaker` accept a `Provider` or a `Backend` instance. When given a Provider, the Component calls `provider.sync()`, `provider.cache()`, `provider.ratelimiter()`, or `provider.breaker()` to build the matching adapter. Add `Provider` base class in `grelmicro.providers._base` with the four factory methods. Add `RedisProvider.ratelimiter()` returning a `RedisRateLimiterAdapter`. The Adapter classes stay public as escape hatches for custom Providers, but the recommended user code uses `Sync(redis)` instead of `Sync(RedisSyncAdapter(provider=redis))`.
* ✨ Add `Grelmicro.current()` classmethod for ambient lookup. Inside `async with micro:` it returns the active app for the current asyncio task.
* ✨ Add `Retry` primitive with decorator, block, and class forms. Five backoff algorithms ship: `ExponentialBackoff` (default, with full jitter), `ConstantBackoff`, `LinearBackoff`, `FibonacciBackoff`, and `RandomBackoff`. `when=` is required and accepts a `Match` (or shorthand). Live reconfiguration via `Reconfigurable[RetryConfig]`. Three-paths configuration. Underlying exception is re-raised with a PEP 678 note on exhaustion. Issue [#165](https://github.com/grelinfo/grelmicro/issues/165).
* ✨ Add `Match` and `Outcome` to `grelmicro.resilience`. `Match` is the resilience-wide outcome filter DSL: `Match.exception(...)`, `Match.result(...)`, `Match.exception_message(...)`, `Match.exception_cause(...)`, `Match.predicate(...)`, `Match.always()`, `Match.never()` plus their `not_*` twins, composed with the `|` and `&` operators. `Outcome[T]` is the dataclass passed to custom predicates (`exception`, `result`, `raised`). Issue [#242](https://github.com/grelinfo/grelmicro/issues/242).
* ✨ Add `grelmicro.providers.redis.RedisProvider`. First-class Redis connection holder shared across components: `RedisProvider("redis://...")`, `RedisProvider(host="...", port=...)`, `RedisProvider()` (env-driven via `REDIS_*`), `RedisProvider.from_config(RedisConfig(...))`, and `RedisProvider.from_client(client, own=False)` for bring-your-own clients. `Grelmicro` dedupes implicit providers by `(provider_class, env_prefix)`, so two adapters with the same prefix share one connection pool. Issue [#226](https://github.com/grelinfo/grelmicro/issues/226).
* ✨ Add `grelmicro.providers.postgres.PostgresProvider`. First-class Postgres connection holder wrapping an `asyncpg.Pool`: `PostgresProvider("postgresql://...")`, `PostgresProvider(host=..., database=..., user=..., password=...)`, `PostgresProvider()` (env-driven via `POSTGRES_*`), `PostgresProvider.from_config(PostgresConfig(...))`, and `PostgresProvider.from_client(pool, own=False)` for bring-your-own pools. Shares the same `(provider_class, env_prefix)` dedupe as `RedisProvider`. Issue [#255](https://github.com/grelinfo/grelmicro/issues/255).

### Breaking

* 💥 Patterns `RateLimiter`, `CircuitBreaker`, and the FastAPI health router resolve through the active `Grelmicro` app. Add the two new Components `RateLimit` (wraps `RateLimiterBackend`, kind `"ratelimiter"`) and `Breaker` (wraps `CircuitBreakerBackend`, kind `"circuitbreaker"`). `Grelmicro.use(...)` auto-wraps a `RateLimiterBackend` or `CircuitBreakerBackend` instance into its matching Component. `HealthChecks` becomes a Component (`kind = "health"`, default `name = "default"`). Pass it to `Grelmicro(uses=[...])` and the FastAPI `health_router()` resolves it via `Grelmicro.current()`. Delete the `rate_limiter_backend_registry`, `circuit_breaker_backend_registry`, `health_checks` registries plus `grelmicro/_backends.py` (`BackendRegistry`, `BackendNotLoadedError`, `BackendAlreadyRegisteredError`). Closes out [#201](https://github.com/grelinfo/grelmicro/issues/201). Issue [#261](https://github.com/grelinfo/grelmicro/issues/261).
* 💥 Redis adapters now take `provider=` or `env_prefix=`, not a positional `url=`. `RedisSyncAdapter`, `RedisCacheAdapter`, and `RedisRateLimiterAdapter` lose their `url=` argument. Pass `provider=RedisProvider(...)` to share a pool, or rely on `env_prefix=` (default `REDIS_`) to build one. Issue [#226](https://github.com/grelinfo/grelmicro/issues/226).
* 💥 `PostgresSyncAdapter` now takes `provider=` or `env_prefix=`, not a positional `url=`. Pass `provider=PostgresProvider(...)` to share a pool, or rely on `env_prefix=` (default `POSTGRES_`) to build one. Issue [#255](https://github.com/grelinfo/grelmicro/issues/255).
* 💥 Rename `TaskManager` to `Tasks`. Class still extends `TaskRouter`, mirroring FastAPI's `APIRouter` ← `FastAPI` shape. Update imports to `from grelmicro.task import Tasks`. Issue [#218](https://github.com/grelinfo/grelmicro/issues/218).
* 💥 Rename `HealthRegistry` to `HealthChecks` (and `HealthRegistryConfig` to `HealthChecksConfig`). Update imports to `from grelmicro.health import HealthChecks`. Issue [#201](https://github.com/grelinfo/grelmicro/issues/201).
* 💥 Rename concrete backends to `*Adapter`. `MemorySyncBackend`, `RedisSyncBackend`, `PostgresSyncBackend`, `SQLiteSyncBackend`, `KubernetesSyncBackend`, `MemoryCacheBackend`, `RedisCacheBackend`, `MemoryRateLimiterBackend`, `RedisRateLimiterBackend`, and `MemoryCircuitBreakerBackend` become `*Adapter`. The `SyncBackend`, `CacheBackend`, `RateLimiterBackend`, and `CircuitBreakerBackend` Protocols stay as-is. Issue [#201](https://github.com/grelinfo/grelmicro/issues/201).
* 💥 Rename nested backoff classes to drop the redundant `Config` suffix. `ExponentialBackoffConfig`, `ConstantBackoffConfig`, `LinearBackoffConfig`, `FibonacciBackoffConfig`, and `RandomBackoffConfig` become `ExponentialBackoff`, `ConstantBackoff`, `LinearBackoff`, `FibonacciBackoff`, and `RandomBackoff`. The `RetryBackoffConfig` discriminated-union alias and the JSON discriminator (`type: "exponential"`) are unchanged. Issue [#239](https://github.com/grelinfo/grelmicro/issues/239).
* 💥 Rename the `Module` protocol to `Component` to avoid clashing with Python's own "module". `ModuleAlreadyRegisteredError` becomes `ComponentAlreadyRegisteredError` and `ModuleNotRegisteredError` becomes `ComponentNotRegisteredError`. The `Sync` and `Cache` classes keep their names. No deprecation shim. Issue [#233](https://github.com/grelinfo/grelmicro/issues/233).
* 💥 Rename the env-loading flag to align the per-call kwarg and the global env var. `read_env=` becomes `env_load=` on every component, `GREL_CONFIG_FROM_ENV` becomes `GREL_ENV_LOAD`, and the `grelmicro._config.env_opt_in_enabled()` helper becomes `env_load_default()`. No deprecation shim. Issue [#232](https://github.com/grelinfo/grelmicro/issues/232).
* 💥 Remove the module-level registry and lifespan API. `grelmicro.lifespan()` is gone. The `register` / `unregister` / `use` / `use_backend` / `use_registry` helpers across `grelmicro.{sync,cache,health,resilience}` (plus the resilience circuit-breaker variants) are gone. Patterns (`Lock`, `TaskLock`, `LeaderElection`, `TTLCache`) now resolve their backend via `Grelmicro.current()` at every call. Build a `Grelmicro(uses=[...])` and open it with `async with micro:`. The `grelmicro.sync._backends` and `grelmicro.cache._backends` modules are removed (sync and cache resolve through the app). The internal `rate_limiter_backend_registry`, `circuit_breaker_backend_registry`, and `health_checks` registries stay private until follow-up issues introduce their Component wrappers. Issue [#207](https://github.com/grelinfo/grelmicro/issues/207).
* 💥 `Retry` outcome filter is now `when=` accepting a `Match`. The old `on=` parameter is gone. The `Match` DSL (`Match.exception(...)`, `Match.result(...)`, `Match.exception_message(...)`, `Match.exception_cause(...)`, `Match.always()`, `Match.never()`, `Match.predicate(...)`) plus their `not_*` twins and the `|`/`&` operators cover the common retry-filter surface. Bare-class shorthand is still accepted (`when=httpx.HTTPError`). Result-based retry lands in the same change: `when=Match.result(None)` retries until the function stops returning `None`. Env var renamed `GREL_RETRY_{NAME}_ON` → `GREL_RETRY_{NAME}_WHEN`. Issue [#242](https://github.com/grelinfo/grelmicro/issues/242).

### Internal

* ⚡ Defer the `opentelemetry` import in `grelmicro.trace`. `import grelmicro.trace` no longer loads `opentelemetry` (was 16 modules). The package is resolved lazily on first call to `instrument`, `span`, or `add_context` and cached. Issue [#189](https://github.com/grelinfo/grelmicro/issues/189).


## 0.21.0 - 2026-05-06

### Breaking

* 💥 Drop Python 3.11. The new floor is `requires-python = ">=3.12"`. RHEL 9 (App Stream `python3.12`) and RHEL 10 (default) ship 3.12 and the UBI images are available, so enterprise users are covered. Issue [#66](https://github.com/grelinfo/grelmicro/issues/66).
* 💥 Drop AnyIO. grelmicro now targets `asyncio` directly. Issue [#183](https://github.com/grelinfo/grelmicro/issues/183).
* 💥 `CircuitBreaker` now takes a backend (``CircuitBreakerBackend``). The in-memory backend (``MemoryCircuitBreakerBackend``) is the default. A future Redis-backed implementation will share state across replicas (issue #188). The async API stays primary, sync code goes through ``cb.from_thread``.
* 💥 The sync adapters on `Lock`, `TaskLock`, `TTLCache`, and `CircuitBreaker` now require the backend to be opened (``async with backend:`` or ``grelmicro.lifespan()``). The backend captures the running loop and the sync adapter dispatches through it. Zero hot-path overhead.
* 💥 Resilience registries are now namespaced. The rate limiter registry name moves from ``"resilience"`` to ``"resilience.ratelimiter"`` and the circuit breaker registry is ``"resilience.circuitbreaker"``. ``grelmicro.lifespan(exclude=...)`` now matches by dotted prefix, so ``exclude={"resilience"}`` still skips both registries.

### Features

* ✨ Add `uvloop` to the `standard` extra (Linux and macOS). Activate with `uvloop.run(main())`.

### Internal

* ✅ Migrate the test suite from `pytest.mark.anyio` to `pytest-asyncio` with `asyncio_mode = "auto"`. AnyIO is no longer a direct dependency of grelmicro (it may still arrive transitively, for example through `fast-depends`).
* ♻️ Adopt PEP 695 generic syntax (`class Foo[T]:`, `def f[T](...)`, `type X = ...`) across `_backends.py`, `_config.py`, `_types.py`, `health/_types.py`, `trace/_instrument.py`, and `tests/task/conftest.py`. Two files keep the older form: the recursive aliases in `_json.py` (ty cannot expand recursive PEP 695 aliases) and the decorator factory in `cache/cached.py` (PEP 695 binds the inner decorator to the outer scope's type parameters, breaking per-decoration-site inference). Issue [#65](https://github.com/grelinfo/grelmicro/issues/65).
* 🔨 Bump `tool.ruff.target-version` to `py312` and the CI matrix to `["3.12","3.13","3.14"]`.

## 0.20.0 - 2026-05-03

Live reconfiguration is complete. Every stateful primitive now exposes `reconfigure(new_config)`, so you can hot-reload from a `ConfigMap` or SIGHUP without restarting the process. See [Live reconfiguration](architecture/reconfigure.md) for the contract.

### Features

* ✨ Add `RateLimiter.reconfigure(new_config)`. Swap algorithm config without rebuilding the limiter. PR [#153](https://github.com/grelinfo/grelmicro/pull/153).
* ✨ Add `reconfigure(new_config)` to `Lock`, `TaskLock`, and `LeaderElection`. Swap timing fields without restarting. The `worker` field cannot change. PR [#159](https://github.com/grelinfo/grelmicro/pull/159).
* ✨ Add `CircuitBreaker.reconfigure(new_config)`. Swap thresholds and `ignore_exceptions` without restarting. Runtime state and `last_error` are kept. `log_level` is applied to the logger. PR [#160](https://github.com/grelinfo/grelmicro/pull/160).
* ✨ Add `HealthRegistry.reconfigure(new_config)`. Swap `cache_ttl` and the default `timeout` without restarting. Per-check timeouts stay as registered. PR [#180](https://github.com/grelinfo/grelmicro/pull/180).

### Docs

* 📝 Reframe README and docs landing as a microservice patterns toolkit. PR [#155](https://github.com/grelinfo/grelmicro/pull/155).
* 📝 Replace "Production-ready" with "Railguarded": 100% pytest coverage, ty-checked, ruff-linted, Pydantic-validated. PR [#181](https://github.com/grelinfo/grelmicro/pull/181).

### Internal

* 🔨 Switch build backend to Hatch. PR [#155](https://github.com/grelinfo/grelmicro/pull/155).
* 🎨 Supersample favicon PNGs with Lanczos downscaling for smoother anti-aliasing. PR [#156](https://github.com/grelinfo/grelmicro/pull/156).

## 0.19.0 - 2026-05-01

Cleans out the long-deprecated APIs (`ResilienceException`, `Synchronization`, `scheduled()`, the `token=` kwarg) ahead of the 1.0.0 design work, ships a 3.4× speedup on env-driven config construction, and brings the test suite under 20s for contributors.

### Breaking

* 💥 The Environmental config path is now opt-in. Set `GREL_CONFIG_FROM_ENV=true` once at startup to enable env reads across every component, or pass `read_env=True` per call. The per-call value (`True`/`False`) always wins over the global flag. This stops grelmicro from silently picking up ambient env vars in unit tests or scripts. Issue [#142](https://github.com/grelinfo/grelmicro/issues/142).
* 💥 The `read_env` kwarg default flips from `True` to `None` on every component. `None` follows the global flag. `True` and `False` keep their meaning as explicit per-call overrides.
* 💥 Remove obsolete deprecation shims that were marked for removal in 0.7.0. Replace `ResilienceException` with `ResilienceError`, `Synchronization` with `SyncPrimitive`, and the `scheduled()` decorator on `TaskRouter` / `TaskManager` with `interval(seconds=N, max_lock_seconds=N*5)`. The `token=` kwarg on `LockAcquireError`, `LockReleaseError`, and `LockNotOwnedError` is removed (drop it from your code). The `sync=` parameter on `interval()` no longer warns when used with non-`Lock` primitives.

### Internal

* ♻️ Add `grelmicro._config.env_opt_in_enabled()` helper that exposes the truthy `GREL_CONFIG_FROM_ENV` check (`1`, `true`, `yes`, `on`, case-insensitive). Issue [#142](https://github.com/grelinfo/grelmicro/issues/142).
* 📝 Document the "no field-mirroring" decision in `docs/architecture/config.md` with the benchmark numbers from `benchmarks/config_attr_benchmark.py`. Closes Issue [#113](https://github.com/grelinfo/grelmicro/issues/113) without code changes: hot-path config reads cost <1% of a real call (~2 ns out of ~250 ns), so we keep `self._config` as the single source of truth instead of copying frozen fields onto the component.
* 🔇 Silence the upstream `testcontainers` `@wait_container_is_ready` deprecation banner via a scoped `filterwarnings` entry in `pyproject.toml`. Replace the unawaited `lambda: sleep(math.inf)` mock side-effect in `tests/sync/test_leaderelection.py::test_leadership_abandon_on_renew_deadline_reached` with an explicit async helper. The full suite now reports zero warnings.
* ⚡ Speed up the test suite from ~73s to ~19s by adding `pytest-xdist` (`-n auto` in `addopts`) and shrinking expiration sleeps in `tests/sync/test_backends.py`. Fix `--durations` reporting by removing the autouse `freeze_time` fixture: `@freeze_time()` decorator stays on the two tests that compare `datetime.now()`, the two tests that previously called `frozen_time.tick(...)` switch to `monkeypatch.setattr(circuitbreaker, "monotonic", ...)`. Issue [#125](https://github.com/grelinfo/grelmicro/issues/125).
* ⚡ Cache the dynamic `BaseSettings` subclass built by `grelmicro._config._build_settings_cls` with `@functools.lru_cache(maxsize=256)`. The env path of `resolve_config` now reuses the same `_<Config>Settings` subclass across calls instead of rebuilding it every time, which makes `Lock("cart")`-style construction ~3.4× faster (232 µs/op → 68 µs/op). The bound is a safety net for long-running processes that might derive prefixes from runtime inputs. Issue [#119](https://github.com/grelinfo/grelmicro/issues/119).
* ♻️ Rename the local `parent_config` to `merged_config` in `_build_settings_cls` and document why the existing `# type: ignore` comments are needed. Issue [#127](https://github.com/grelinfo/grelmicro/issues/127).

## 0.18.0 - 2026-04-30

M2 milestone closed: backend wiring is now fully explicit. Construction is pure (no global writes), registration is named (`<module>.register(backend, "name")`), and `grelmicro.lifespan(*ad_hoc, exclude=...)` walks every registry that has been imported and opens its registered backends in one call. Task-scoped overrides via `with <module>.use(...):` swap backends per request or per test through `contextvars`.

### Breaking

* 💥 Backend constructors are now pure: `__init__` performs no registry writes. The `auto_register` kwarg is removed from every backend and from `HealthRegistry`. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* 💥 `BackendRegistry.set` is renamed `register` and `BackendRegistry.unregister` is added with an identity check. `reset` remains for test fixtures. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* 💥 `async with backend` opens the connection but no longer registers. Call `register(backend)` (or `use_backend(backend)`) to register, or open everything at once with `grelmicro.lifespan()`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 `BackendRegistry` is now multi-name: `register(backend, name="default")`, `unregister(name, backend=None)`, `get(name="default")`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 The sync registry name changed from `"lock"` to `"sync"` (used in error messages and `lifespan()` exclude keys). The rate limiter registry changed from `"rate_limiter"` to `"resilience"`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 Overwriting a registered name with a different instance now raises `BackendAlreadyRegisteredError` (was: warning + replace). Re-registering the same instance stays a no-op. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).

### Features

* ✨ Add `grelmicro.sync.use_backend`, `grelmicro.cache.use_backend`, `grelmicro.resilience.use_backend`, and `grelmicro.health.use_registry` for explicit, idempotent process-lifetime registration. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* ✨ `grelmicro.lifespan(*ad_hoc, exclude=...)` opens every registered backend across every imported module in one call, with reverse-order shutdown. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Per-module helpers `register`, `unregister`, `use_backend`, `use` on `grelmicro.sync`, `grelmicro.cache`, `grelmicro.resilience` (and `use_registry`, `use` on `grelmicro.health`). PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Task-scoped overrides via `<module>.use(backend)` or `<module>.use(default=a, analytics=b)`. Stacks LIFO via `contextvars` for per-test and per-request substitution. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Primitives accept `backend=` as either a backend instance or a registered name (`Lock("audit", backend="analytics")`). The registry is consulted on each call so `<module>.use(...)` overrides apply. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Registries subscribe themselves on import: `lifespan()` walks only modules that are actually imported, so unused components have zero RAM cost and zero startup work. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Lookup falls back to the sole registered entry when no `"default"` is named, so the single-backend case stays one-call. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).

## 0.17.0 - 2026-04-29

### Breaking

* 💥 `CircuitBreaker` config moves to a frozen `CircuitBreakerConfig`. Read it via `cb.config`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* 💥 The mutable attributes `cb.error_threshold`, `cb.success_threshold`, `cb.reset_timeout`, `cb.half_open_capacity`, `cb.ignore_exceptions`, `cb.log_level` are removed. Construct a new `CircuitBreaker` to change config. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* 💥 Rename `grelmicro.logging` to `grelmicro.log` and `grelmicro.tracing` to `grelmicro.trace`. Avoids shadowing stdlib `logging` and aligns with the OpenTelemetry / `ddtrace` `trace` (singular) convention. Update imports: `from grelmicro import log, trace`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `configure_logging()` is renamed `log.configure()`. Use `log.configure_with(config)` for the declarative path. Both return the applied `LoggingConfig`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingSettings` (the `BaseSettings` shadow class) is removed. `LoggingConfig` is the config class. Env reading happens inside `log.configure()`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingConfig` field names move to lowercase: `LOG_BACKEND` → `backend`, `LOG_LEVEL` → `level`, `LOG_FORMAT` → `format`, `LOG_TIMEZONE` → `timezone`, `LOG_JSON_SERIALIZER` → `json_serializer`, `LOG_CALLER_ENABLED` → `caller_enabled`, `LOG_OTEL_ENABLED` → `otel_enabled`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 Env vars move from `LOG_*` to `GREL_LOG_*` to align with the rest of the library. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingSettingsValidationError` is removed. `pydantic.ValidationError` propagates from `log.configure()` like every other component. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).

### Features

* ✨ Add `CircuitBreakerConfig` and `CircuitBreaker.from_config(name, config)`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `CircuitBreaker` reads `GREL_CIRCUIT_BREAKER_<NAME>_*` env vars and accepts `env_prefix=` / `read_env=`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `ignore_exceptions` accepts fully-qualified import strings (`"builtins.ValueError"`) so YAML and env loaders can specify exception types. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ Env vars for tuple/list fields accept comma-separated values in addition to JSON arrays. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `log.configure(**kwargs)` accepts every `LoggingConfig` field as a kwarg, mirroring the three-paths contract used by other components. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* ✨ `log.configure_with(config)` is the declarative entry point. Returns the applied `LoggingConfig`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).

### Internal

* ♻️ Add `grelmicro/_types.py` for shared lightweight type aliases (`LogLevel`). PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ♻️ Add `grelmicro/_config.py::parse_csv_or_json` shared utility for env var list parsing. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).

### Docs

* 🎨 Switch logo typeface from Funnel Sans to Funnel Display.

## 0.16.1 - 2026-04-29

### Internal

* ✅ "No registry call at construction" tests now patch the registry source instead of the per-module import alias, so a future refactor that bypasses the local alias can no longer silently pass the check. PR [#130](https://github.com/grelinfo/grelmicro/pull/130).
* ⬆️ Bump `ty` from 0.0.29 to 0.0.30. PR [#111](https://github.com/grelinfo/grelmicro/pull/111).
* ⬆️ Pre-commit autoupdate. PR [#114](https://github.com/grelinfo/grelmicro/pull/114).

## 0.16.0 - 2026-04-29

### Breaking

* 💥 `LockConfig`, `TaskLockConfig`, `LeaderElectionConfig`, and `RateLimiterConfig` no longer carry a `name` field. Pass the name positionally: `Lock("cart", LockConfig(lease_duration=30))`. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 Rename `TokenBucket` to `TokenBucketConfig` and `GCRA` to `GCRAConfig`. `RateLimiterConfig` becomes the discriminated union of algorithm configs. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 `RateLimiter` takes the algorithm config positionally: `RateLimiter("api", GCRAConfig(limit=100, window=60))`. The `algorithm=`, `limit=`, `window=` kwargs are removed. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 `fail_open` moves from `RateLimiter(...)` to the algorithm config. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).

### Features

* ✨ Add `Component.from_config(name, config)` to every primitive (`Lock`, `TaskLock`, `LeaderElection`, `RateLimiter`, `HealthRegistry`, `RateLimitFilter`, `DuplicateFilter`). PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Read environment variables under `GREL_<COMPONENT>_<NAME>_*` for every component that supports the environmental path. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Add `RateLimiter.token_bucket(name, ...)` and `RateLimiter.gcra(name, ...)` factory classmethods. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Add `env_prefix=` and `read_env=` kwargs to every component that exposes the environmental path. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Normalise instance names like `payments-eu`, `cart.v2`, or `weather/svc` into POSIX env var segments. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).

### Changed

* ♻️ `Lock`, `TaskLock`, `LeaderElection`, and `RateLimiter` now resolve the backend lazily on first use instead of at construction. `BackendNotLoadedError` surfaces on the first `acquire`/`peek`/`reset` call rather than in `__init__`. Each component exposes a public `backend` property. PR [#128](https://github.com/grelinfo/grelmicro/pull/128).

### Fixed

* 🐛 Auto-registered backends now identity-check before clearing the registry on `__aexit__`, so a replacement instance is left alone. PR [#122](https://github.com/grelinfo/grelmicro/pull/122).
* 🐛 `Lock.release` clears local ownership only after the backend confirms the release. PR [#122](https://github.com/grelinfo/grelmicro/pull/122).

## 0.15.0 - 2026-04-29

### Breaking

* 💥 Redesign the `health` module: `@health.check("name")` decorator, binary `ok`/`error` status, empty probe bodies, per-check caching. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 Endpoint renames: `/health/live` → `/livez`, `/health/ready` → `/readyz`. New `/healthz` returns the full check JSON. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthRegistry.check()` renamed to `run()`. The `check` name is now the decorator. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthChecker` Protocol removed. Use plain `def` or `async def` functions. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthReport.components: list` becomes `HealthReport.checks: dict[name, ...]`. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthCheckTimeoutError` and the three-state `HealthStatus` removed. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).

### Docs

* 📝 Restate the versioning policy: pre-1.0 `MINOR` may break, `PATCH` never. Post-1.0 deprecations get two `MINOR` releases.

## 0.14.3 - 2026-04-22

### Docs

* 🐛 Fix the wordmark duplicating on PyPI and other renderers that don't understand GitHub's theme-only URL fragments. PR [#109](https://github.com/grelinfo/grelmicro/pull/109).

## 0.14.2 - 2026-04-22

### Docs

* 🐛 Fix the landing-page wordmark disappearing when the docs site is toggled into dark mode. PR [#108](https://github.com/grelinfo/grelmicro/pull/108).
* 📝 Centre the badges row under the tagline. PR [#108](https://github.com/grelinfo/grelmicro/pull/108).

## 0.14.1 - 2026-04-22

### Docs

* 🎨 Ship the grelmicro brand identity: wordmark, favicon, and social-preview card. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 🎨 Refresh the docs theme with the brand palette. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 📝 Rewrite the "Why grelmicro" pillars. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 📝 Split the resilience docs into per-pattern pages.
* 📝 Add an Installation guide with `pip`, `uv`, and `poetry` tabs.
* 📝 Render PEP 727 `Annotated[..., Doc(...)]` parameter docs via `griffe-typingdoc`.
* 📝 Plain-English pass on docs and docstrings for non-native readers.
* 📝 Add a Mermaid state diagram to the Circuit Breaker page.
* 📝 Document every `__all__` symbol in the API reference.
* 📝 Add a plain-English style guide to `CONTRIBUTING.md`.

### Internal

* 🐛 De-flake `test_lock_reentrant_from_thread` on Python 3.12. Fixes [#105](https://github.com/grelinfo/grelmicro/issues/105).
* 🔧 Add keywords to `pyproject.toml` for PyPI discovery.

## 0.14.0 - 2026-04-21

### Features

* ✨ Add pluggable `RateLimiter` algorithms via the `algorithm=` parameter: `TokenBucket` and `GCRA`. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `MemoryTokenBucket`, a standalone synchronous token-bucket primitive. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `RateLimitFilter`, a `logging.Filter` with configurable `key_mode`. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `DuplicateFilter`, a `logging.Filter` that caps repeated records per key with optional TTL. PR [#94](https://github.com/grelinfo/grelmicro/pull/94).
* ✨ `HealthRegistry` now logs every unhealthy path at `WARNING` (`ERROR` for unexpected exceptions). PR [#92](https://github.com/grelinfo/grelmicro/pull/92).

### Deprecations

* 🗑️ `RateLimiter(name, limit=..., window=...)` is deprecated. Use `RateLimiter(name, algorithm=GCRA(limit=..., window=...))` instead. Will be removed in 0.15.0. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).

### Docs

* 📝 Add [`CONTRIBUTING.md`](https://github.com/grelinfo/grelmicro/blob/main/CONTRIBUTING.md) with repo conventions. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* 📝 Add a "Choosing an algorithm" guide for `TokenBucket` vs `GCRA` in the [Rate Limiter](resilience/rate-limiter.md) docs. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* 📝 Surface `THIRD_PARTY_NOTICES.md` in the docs site. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).

### Security

* 🔒️ Harden CI supply chain: pin all Actions to SHAs, close `run:` injection vectors, add zizmor workflow-lint, restrict Dependabot auto-merge to uv patch/minor updates. PRs [#95](https://github.com/grelinfo/grelmicro/pull/95), [#100](https://github.com/grelinfo/grelmicro/pull/100), [#101](https://github.com/grelinfo/grelmicro/pull/101).

### Internal

* ⬆️ Bump `pydantic` to 2.13.0, `opentelemetry-api` / `opentelemetry-sdk` to 1.41.0, `pytest` to 9.0.3, `ruff` to 0.15.10, `ty` to 0.0.29, `fastapi` to 0.135.3, `uvicorn` to 0.44.0. PR [#99](https://github.com/grelinfo/grelmicro/pull/99).
* ⬆️ Bump `pydantic-extra-types` from 2.11.1 to 2.11.2. PR [#89](https://github.com/grelinfo/grelmicro/pull/89).
* ⬆️ Pre-commit `ruff` autoupdate (v0.15.9 → v0.15.11). PR [#91](https://github.com/grelinfo/grelmicro/pull/91).
* ⬆️ Bump `codecov/codecov-action` to v6. PR [#96](https://github.com/grelinfo/grelmicro/pull/96).
* ⬆️ Bump `astral-sh/setup-uv` to v8. PR [#97](https://github.com/grelinfo/grelmicro/pull/97).
* ⬆️ Bump `dependabot/fetch-metadata` to v3. PR [#98](https://github.com/grelinfo/grelmicro/pull/98).

## 0.13.0 - 2026-04-08

### Features

* ✨ Add `RateLimiter.peek(key)`: check rate limit state without consuming tokens. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).
* ✨ Add `RateLimiter.reset(key)`: delete rate limit state for a key, restoring full quota. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).
* ✨ Add `fail_open` parameter to `RateLimiter`: return allowed result on backend errors instead of propagating exceptions. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).

## 0.12.0 - 2026-04-07

### Features

* ✨ Add `health` module with health check registry, concurrent checker execution, and FastAPI integration for liveness/readiness probes. PR [#84](https://github.com/grelinfo/grelmicro/pull/84).

### Internal

* ⬆️ Bump orjson from 3.11.7 to 3.11.8. PR [#72](https://github.com/grelinfo/grelmicro/pull/72).
* ⬆️ Bump ty from 0.0.26 to 0.0.27. PR [#74](https://github.com/grelinfo/grelmicro/pull/74).
* ⬆️ Update uv-build requirement from `<0.10.0` to `<0.12.0`. PR [#75](https://github.com/grelinfo/grelmicro/pull/75).
* 👷 Add build provenance attestations and wheel verification to release pipeline.
* ♻️ Pre-release cleanup: add health/json to overview, fix style inconsistencies, remove stale branches.

## 0.11.0 - 2026-04-03

### Breaking Changes

* 💥 **Logging**: split `caller` into separate `logger` (logger name) and `caller` (`function:line`) fields. `caller` is now opt-in via `GREL_LOG_CALLER_ENABLED` (default: `False`), following common structured-logging conventions. Uvicorn formatter never includes `caller`.
* 💥 **Cache**: replace `TTLCache` `serializer`/`deserializer` callable pair with a single `serializer` accepting a `CacheSerializer` protocol object. Use `JsonSerializer()`, `PydanticSerializer(Model)`, or `PickleSerializer()` instead.

### Features

* ✨ Add `GREL_LOG_CALLER_ENABLED` setting to opt in to caller info (`function:line`) in log records. Disabled by default for cleaner logs and better performance.
* ✨ Add `logger` field (logger name, e.g., `myapp.api`) to all log records across all backends and formats.
* ✨ Add `grelmicro.json` module with fast JSON serialization using `orjson` when available, with automatic fallback to stdlib `json`.

## 0.10.0 - 2026-04-02

### Features

* ✨ Add `RateLimiter` to the `resilience` module: Redis-backed sliding-window rate limiting using the GCRA algorithm. Includes `RateLimitResult` with fields mapping to IETF rate limit headers, weighted requests via `cost` parameter, and `RateLimitExceededError`.

### Removals

* 🗑️ Remove deprecated `UvicornJSONFormatter` and `UvicornAccessJSONFormatter`. Use `UvicornFormatter` and `UvicornAccessFormatter` instead (deprecated since 0.9.1).

### CI

* ⚡ Migrate PyPI publishing from API token to OIDC trusted publishing.

## 0.9.1 - 2026-04-01

### Deprecations

* 🗑️ **`UvicornJSONFormatter` and `UvicornAccessJSONFormatter` are deprecated.** Use `UvicornFormatter` and `UvicornAccessFormatter` instead. The new formatters respect `GREL_LOG_FORMAT` instead of always producing JSON. Old names kept as aliases with `DeprecationWarning`.

## 0.9.0 - 2026-04-01

### Breaking Changes

* 💥 **`GREL_LOG_FORMAT` default changed from `JSON` to `AUTO`.** In production (non-TTY), behavior is identical (JSON output). In local dev (TTY), output switches to human-readable `TEXT` with colors. Set `GREL_LOG_FORMAT=JSON` explicitly to restore the previous default.

### Features

* ✨ Add `AUTO` log format (new default): detects TTY and selects `TEXT` (terminal) or `JSON` (piped/CI).
* ✨ Add `LOGFMT` log format: key-value pairs following the [logfmt](https://brandur.org/logfmt) convention, 30-40% smaller than JSON.
* ✨ Add `PRETTY` log format: multi-line indented output with structured error rendering.
* ✨ Enhanced `TEXT` format: now includes extra context fields as `key=value` pairs and supports ANSI colors.
* ✨ Add `NO_COLOR` / `FORCE_COLOR` environment variable support following [no-color.org](https://no-color.org) standard.

## 0.8.0 - 2026-04-01

### Breaking Changes

* 💥 **Backend imports moved to submodules.** Use `from grelmicro.sync.redis import RedisSyncBackend` instead of `from grelmicro.sync import RedisSyncBackend`. Same for all sync, cache, and logging backends. See [Import Strategy](architecture/imports.md).

### Features

* ✨ Add Uvicorn JSON formatters (`UvicornJSONFormatter`, `UvicornAccessJSONFormatter`) for structured logging via `dictConfig`.

## 0.7.0 - 2026-03-31

### Breaking Changes

* 💥 **Logging JSON format redesigned** to follow industry standards:
    * `logger` renamed to `caller`
    * `thread` removed
    * `ctx` removed: extra fields are now flat at the top level
    * `exception` replaced by structured `error` object (`type`, `message`, `stack`)

### Features

* ✨ Add `tracing` module with `@instrument` decorator, `span()` context manager, and `add_context()` for unified logging and OTel instrumentation.

### Performance

* ⚡ **Logging**: Up to +23% throughput across all backends.
* ⚡ Use `OrderedDict` for O(1) LRU operations in `TTLCache`.

### Refactors

* ♻️ Extract shared Redis config into `grelmicro/_redis.py`.
* ♻️ Make `TTLCache` generic and add `Doc` annotations.
* ♻️ Extract context stack into `grelmicro/_context.py` to decouple logging from tracing.
* ♻️ Filter private (`_`-prefixed) attributes from stdlib JSON log output.
* ♻️ Widen `@instrument(skip=...)` type from `set[str]` to `AbstractSet[str]`.

### Removals

* 🗑️ `Synchronization` protocol removed. Use `SyncPrimitive` instead (deprecated since 0.6.0).
* 🗑️ `ResilienceException` removed. Use `ResilienceError` instead (deprecated since 0.6.0).
* 🗑️ The `token` parameter on lock errors removed (deprecated since 0.6.0).
* 🗑️ The `sync` parameter on `interval()` removed (deprecated since 0.6.0).
* 🗑️ The `scheduled()` decorator removed (deprecated since 0.6.0).

## 0.6.0 - 2026-03-30

### Deprecations

* 🗑️ `Synchronization` protocol renamed to `SyncPrimitive`. The old name still works but emits a `DeprecationWarning`. Will be removed in 0.7.0.
* 🗑️ `ResilienceException` renamed to `ResilienceError`. The old name still works but emits a `DeprecationWarning`. Will be removed in 0.7.0.
* 🗑️ The `token` parameter on `LockAcquireError`, `LockReleaseError`, and `LockNotOwnedError` is deprecated. Tokens are no longer included in error messages for security. Will be removed in 0.7.0.
* 🗑️ The `sync` parameter on `interval()` for `TaskLock` and `LeaderElection` is deprecated. Use `max_lock_seconds` and `leader` parameters instead. Will be removed in 0.7.0.
* 🗑️ The `scheduled()` decorator is deprecated. Use `interval()` with `max_lock_seconds` or `leader` instead. Will be removed in 0.7.0.

### Features

* ✨ Add in-memory [TTL cache](cache.md) with LRU eviction, per-key stampede protection, and `@cached` decorator.
* ✨ Add `RedisCacheBackend` for distributed cache storage.
* ✨ Add cache statistics via `CacheInfo` (hits, misses, evictions, stampedes).
* ✨ Add Kubernetes sync backend using Lease resources (`pip install grelmicro[kubernetes]`).
* ✨ Add SQLite sync backend for home lab and local testing (`pip install grelmicro[sqlite]`).

### Security

* 🔒️ Remove token values from lock error messages to prevent leaking in logs.
* 🔒️ Upgrade `requests` to 2.33.0 (CVE fix in transitive dependency).

### Refactors

* ♻️ Unify error hierarchy under `GrelmicroError` base class. All module errors (`SyncError`, `ResilienceError`, `LoggingError`, `TaskError`, `CacheError`) now share a common base.
* ♻️ Use server-side timestamps and native Lease fields in sync backends.
* ♻️ Simplify token generation from UUID-based to string concatenation.
* ♻️ Harden TaskLock token nonce and error handling.

### Internal

* ✅ Achieve 100% library code coverage.
* 💚 Fix flaky integration test timeout in CI.
* ⬆️ Bump dependencies and fix ty v0.0.26 type errors.

### Docs

* 📝 Add [cache module](cache.md) documentation with usage guide and API reference.
* 📝 Add [Kubernetes Backend Architecture](architecture/kubernetes.md) page.
* 📝 Add [SQLite Backend Architecture](architecture/sqlite.md) page.
* 📝 Add backend comparison matrix to [Coordination](coordination.md#backends) guide.
* 📝 Rewrite README with project vision.

## 0.5.0 - 2026-03-17

### Breaking Changes

* 💥 Add namespace prefix to sync primitive backend keys (`lock:`, `tasklock:`, `leader:`). See [Migration Guide](#migration-guide) below.

### Features

* ✨ Add `TaskLock.from_thread` thread-safe adapter. PR [#57](https://github.com/grelinfo/grelmicro/pull/57).
* ✨ Add specific lock error classes (`LockAcquireError`, `LockReleaseError`, `LockLockedCheckError`, `LockOwnedCheckError`, `LockReentrantError`). PR [#57](https://github.com/grelinfo/grelmicro/pull/57).

### Refactors

* ♻️ Consolidate distributed lock and leader gating into the `interval()` decorator via `max_lock_seconds` and `leader` parameters. PR [#54](https://github.com/grelinfo/grelmicro/pull/54).

### Docs

* 📝 Add [Coordination Architecture](architecture/coordination.md) page. PR [#57](https://github.com/grelinfo/grelmicro/pull/57).

### Internal

* ⬆️ Bump redis, fastapi, pydantic, and pydantic-settings. PR [#55](https://github.com/grelinfo/grelmicro/pull/55).
* ⬆️ Update pre-commit hooks. PR [#50](https://github.com/grelinfo/grelmicro/pull/50).

### Migration Guide

#### Namespace-Prefixed Backend Keys

Prior versions used the `name` parameter directly as the backend key. Now each primitive adds a type-specific prefix:

| Primitive | Name | Backend Key |
|---|---|---|
| `Lock("my-resource")` | `my-resource` | `lock:my-resource` |
| `TaskLock("cleanup")` | `cleanup` | `tasklock:cleanup` |
| `LeaderElection("main")` | `main` | `leader:main` |

Existing locks stored in Redis or PostgreSQL will no longer match after upgrading. A running instance on the old version and one on the new version will **not** see each other's locks.

Upgrade all running instances together so they use the same key format. Old keys expire automatically via their lease duration (Redis `PEXPIRE` / PostgreSQL `expire_at`).

## 0.4.1 - 2026-03-13

### Docs

* 📝 Add Task Lock to synchronization primitives guide.

### Internal

* ⬆️ Bump actions/checkout to v6 and astral-sh/setup-uv to v7.

## 0.4.0 - 2026-03-13

### Features

* ✨ Add `TaskLock` for distributed task locking with auto-renewal.
* ✨ Add `GREL_LOG_TIMEZONE` support for configurable timezone in logging output.
* ✨ Add OpenTelemetry trace context injection into log records.
* ✨ Add `structlog` as alternative logging backend.
* ✨ Add configurable JSON serializer (`json` / `orjson`) for logging.

### Docs

* 📝 Add logging benchmark and performance documentation.

### Internal

* ⬆️ Bump orjson from 3.11.5 to 3.11.6. PR [#51](https://github.com/grelinfo/grelmicro/pull/51).
* ⬆️ Bump freezegun from 1.5.2 to 1.5.5. PR [#33](https://github.com/grelinfo/grelmicro/pull/33).

## 0.3.2 - 2026-01-27

### Internal

* 👷 Migrate from mypy to Astral ty for type checking. PR [#45](https://github.com/grelinfo/grelmicro/pull/45).
* 🔧 Add Python 3.14 support. PR [#47](https://github.com/grelinfo/grelmicro/pull/47).
* 🔧 Switch build system to `uv_build`. PR [#49](https://github.com/grelinfo/grelmicro/pull/49).
* 💚 Simplify CI and release workflow. PR [#24](https://github.com/grelinfo/grelmicro/pull/24).

## 0.3.1 - 2025-06-05

### Docs

* 📝 Add resilience patterns section and update links in README and index.

### Internal

* 💚 Fix release pipeline and GitHub Pages deployment permissions.

## 0.3.0 - 2025-06-05

### Features

* ✨ Add Circuit Breaker resilience pattern. PR [#18](https://github.com/grelinfo/grelmicro/pull/18).

### Docs

* 📝 Refactor code examples to use snippets.

### Internal

* 👷 Add Dependabot configuration for weekly updates.
* 🔒️ Fix workflow permission issues. PR [#21](https://github.com/grelinfo/grelmicro/pull/21), [#22](https://github.com/grelinfo/grelmicro/pull/22).

## 0.2.3 - 2024-12-04

### Features

* ✨ Add Redis key prefix support to avoid conflicts in shared instances.
* ✨ Add Redis and PostgreSQL settings management from environment variables.

## 0.2.2 - 2024-11-28

### Features

* ✨ Add PostgreSQL backend configuration from environment variables.

### Internal

* 🐛 Fix release workflow. PR [#7](https://github.com/grelinfo/grelmicro/pull/7), [#9](https://github.com/grelinfo/grelmicro/pull/9).

## 0.2.1 - 2024-11-26

### Internal

* 💚 Set up release workflow with version tagging.

## 0.2.0 - 2024-11-26

First public release.

### Features

* ✨ Add distributed `Lock` with lease-based expiration.
* ✨ Add `LeaderElection` for single-leader task execution.
* ✨ Add `IntervalTask` scheduler for periodic tasks with synchronization support.
* ✨ Add Redis, PostgreSQL, and in-memory synchronization backends.
* ✨ Add logging module with JSON and TEXT formatting via `GREL_LOG_LEVEL` and `GREL_LOG_FORMAT` environment variables.

### Docs

* 📝 Add MkDocs documentation site with Material theme.

### Internal

* 👷 Add unified CI workflow with linting, testing, and coverage.
