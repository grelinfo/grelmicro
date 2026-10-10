# Migration

One note per minor release, listing only what you have to change. Each entry
names the symptom first, so you can match what you are seeing rather than
read the whole page.

This page covers **0.30 onward**. For an older version, read the `Breaking`
sections in the [changelog](changelog.md), newest first.

In `0.x` the minor is the breaking position: a `0.x.0` release may change the
public API, and a `0.x.y` release is a safe patch. So an upgrade within one
minor rarely needs this page. Two patches are exceptions, and neither changes
any API: [0.32.2](#0-32-2-circuit-breaker-state) changes what an already-open
circuit does once, and [0.37.1](#0-37-1-url-validation) refuses a provider URL
that a client library used to accept.

## Find your symptom

| Symptom | Release | Fix |
|---|---|---|
| `AttributeError: 'Operation' object has no attribute 'response'` | 0.30 | [Use `result()`](#0-30-operation-result) |
| A password or URL reads back as `SecretStr(...)` or `***` | 0.31, 0.32 | [Call `.get_secret_value()`](#0-31-secret-credentials) |
| Metrics stopped arriving after upgrading, with no error | 0.31 | [Set an endpoint](#0-31-metrics-auto-exporter) |
| `TypeError` on a SQLite adapter, either `unexpected keyword argument 'path'` or `takes 1 positional argument` | 0.32 | [Pass `provider=`](#0-32-sqlite-provider) |
| An open circuit closed once, just after upgrading | 0.32.2 | [Expected, happens once](#0-32-2-circuit-breaker-state) |
| `TypeError: @cached on ... needs an explicit key=` | 0.34 | [Name the key](#0-34-cached-method-key) |
| Task run totals jumped after upgrading, with no new failures | 0.34 | [Filter on `outcome`](#0-34-task-run-outcomes) |
| `ImportError: cannot import name 'LogTimeZoneType'` | 0.36 | [Use `TimeZoneName`](#0-36-timezone-type) |
| `SettingsValidationError: unknown timezone name` on a value like `CEST` | 0.36 | [Name the zone](#0-36-timezone-abbreviation) |
| `ImportError: cannot import name 'RateLimiterRegistry'` or `ImportError: cannot import name 'CircuitBreakerRegistry'` | 0.37 | [Rename to `Component`](#0-37-registry-renamed) |
| `TypeError: health_router() got an unexpected keyword argument 'registry'` | 0.37 | [Pass `component=`](#0-37-registry-renamed) |
| A provider error on a URL that used to connect | 0.37.1 | [Fix the URL](#0-37-1-url-validation) |
| `ModuleNotFoundError: No module named 'grelmicro.clientip'` | 0.39 | [Import from `grelmicro.security`](#0-39-clientip-moved) |
| A logging filter or level set on `grelmicro.clientip` stopped matching | 0.39 | [Rename the logger](#0-39-clientip-moved) |
| `ImportError: cannot import name 'LogSettingsValidationError'`, or any other `*SettingsValidationError` | 0.40 | [Catch the base error](#0-40-one-settings-error) |
| `SettingsValidationError` where you caught `pydantic.ValidationError` from `Fallback`, `Shield`, or `TTLCache` | 0.40 | [Catch the base error](#0-40-one-settings-error) |
| `SettingsValidationError` where you caught `ValueError` or `TypeError` from `cached()`, `TrustedProxies`, or `ExternalConfig` | 0.40 | [Catch the base error](#0-40-one-settings-error) |
| `SettingsValidationError` on a bad `Lock` name, adapter `table_name`, or Redis `prefix` | 0.40 | [Catch the base error](#0-40-one-settings-error) |
| `SettingsValidationError: environment= must be one of ...` on a `Grelmicro(...)` that used to build | 0.40 | [Name a real tier](#0-40-environment-validated) |
| `ValueError` where you caught `TypeError` from any `Match` argument error or a bad `when=` | 0.40 | [Catch `ValueError`](#0-40-match-value-error) |
| `ImportError: cannot import name 'RedisProviderConfigError'` or `'PostgresProviderConfigError'` | 0.40 | [Catch the base error](#0-40-provider-errors) |
| A `@retry` or `@fallback` on a callable object never engaged, or `@measure` recorded almost no time for it | 0.41 | [Nothing to change](#0-41-async-callable-objects) |
| `TypeError: ... only decorates async functions` from `@timeout`, `@bulkhead` or `@shield` on a callable object | 0.41 | [Nothing to change](#0-41-async-callable-objects) |
| `TypeError: @cached(ttl=...) supports async functions only`, or `AttributeError: '...' object has no attribute '__qualname__'` from `@cached` | 0.41 | [Nothing to change](#0-41-async-callable-objects) |
| `EventLoopDeadlockError` from a `CircuitBreaker` on a callable object, which an `except Exception` did not catch | 0.41 | [Nothing to change](#0-41-async-callable-objects) |
| `TypeError: ... got an unexpected keyword argument 'lock'` or `'leader'` from `every` | 0.42 | [Pass `gate=`](#0-42-task-gate) |
| A cron task runs on every replica after upgrading | 0.42 | [Pass `gate="claim"`](#0-42-task-gate) |
| `SettingsValidationError: lock= takes 'process', 'host', 'cluster' or None` | 0.42 | [Name the fold scope](#0-42-fold-scope) |
| `OutOfContextError` from a `@cached` function or `get_or_set` with `lock="cluster"` | 0.42 | [Register a lock backend](#0-42-fold-scope) |
| A `get_or_set` value computed once per replica after upgrading | 0.42 | [Pass `lock="cluster"`](#0-42-fold-scope) |
| `TypeError: ... got an unexpected keyword argument 'wait_timeout'` | 0.42 | [Pass `max_wait=`](#0-42-max-wait) |
| `SettingsValidationError` on `BulkheadConfig.max_wait` set to `None` | 0.42 | [Pass `0`](#0-42-max-wait) |
| `BackendScopeError: ... is bound to InProcessLock` on `Idempotency` or `IdempotentRequests` | 0.42 | [Register a lock backend](#0-42-idempotency-lock) |
| `CachedResponses` misses run once per replica after upgrading | 0.42 | [Pass `lock="cluster"`](#0-42-idempotency-lock) |
| `ImportError: cannot import name 'metrics_router' from 'grelmicro.metrics'` | 0.42 | [Import from `grelmicro.integrations.fastapi`](#0-42-metrics-router-moved) |
| `ModuleNotFoundError: No module named 'grelmicro.metrics.fastapi'` | 0.42 | [Import from `grelmicro.integrations.fastapi`](#0-42-metrics-router-moved) |
| `ImportError: cannot import name 'CacheError' from 'grelmicro.cache'` | 0.42 | [Delete the `except CacheError:` block](#0-42-cache-error) |
| `SettingsValidationError: Could not validate settings: min_hold_duration must be greater than or equal to interval` | 0.42 | [Hold the claim for the interval](#0-42-task-gate) |
| `SettingsValidationError` or `ValueError`: `... must be whole seconds or a timedelta` | 0.42 | [Pass whole seconds or a `timedelta`](#0-42-durations) |
| `TypeError: TaskRouter.every() got an unexpected keyword argument 'seconds'` | 0.42 | [Pass `interval=`](#0-42-durations) |
| `TypeError: ... got an unexpected keyword argument 'misfire_grace_seconds'` | 0.42 | [Pass `misfire_grace=`](#0-42-durations) |
| `TypeError: ... declares cache=60` on a route | 0.42 | [Pass `True` or a `timedelta`](#0-42-durations) |
| `TypeError: ... got an unexpected keyword argument 'ignore_exceptions'` from a `CircuitBreaker` | 0.42 | [Pass `when=`](#0-42-when) |
| `TypeError: ... got an unexpected keyword argument 'timeout_errors'` from a `Shield` | 0.42 | [Pass `when=`](#0-42-when) |
| `SettingsValidationError: ... when: Field required` from a `Shield` | 0.42 | [Pass `when=`](#0-42-when) |
| `TypeError: '_ShieldDecorator' object is not callable` from a bare `@shield` | 0.42 | [Pass a preset and `when=`](#0-42-when) |
| `AttributeError: 'TaskLock' object has no attribute 'refresh'` | 0.42 | [Call `extend()`](#0-42-lock-extend) |
| `LockExtendError` where you caught `LockAcquireError` or `LockReleaseError` from extending a lease | 0.42 | [Catch `LockExtendError`](#0-42-lock-extend) |
| `LockNotOwnedError` passes an `except LockReleaseError` or `except LockBackendError` | 0.42 | [Catch `LockNotOwnedError`](#0-42-lock-extend) |
| A dashboard or alert on `grelmicro.lock.renewals` stopped receiving data | 0.42 | [Use `grelmicro.lock.extensions`](#0-42-lock-extend) |
| `TypeError: key must be a function, use key_template= for a template` from `@cached` or `@limiter` | 0.42 | [Pass `key_template=`](#0-42-key-function) |
| `TypeError: ... got an unexpected keyword argument 'key_maker'` | 0.42 | [Pass `key=`](#0-42-key-function) |
| `ImportError: cannot import name 'IdempotencyKeyMakerError'` | 0.42 | [Catch `IdempotencyKeyFunctionError`](#0-42-key-function) |
| `SettingsValidationError` naming `GREL_SHIELD_{NAME}_PROFILE` or `GREL_SHIELD_PROFILE`: `is no longer read` | 0.42 | [Choose the preset in code](#0-42-shield-profile-env) |
| `TypeError: AuthenticatedRequests.from_config takes the verifier first: pass from_config(verifier, config)` | 0.42 | [Pass the verifier first](#0-42-from-config) |
| `SettingsValidationError: TaskLock has no name` | 0.42 | [Name the lock or gate a task with it](#0-42-lock-names) |
| `SettingsValidationError: Invalid lock name ...` from a `TaskLock`, a `LeaderElection` or a gated task | 0.42 | [Pass a valid name](#0-42-lock-names) |
| `SettingsValidationError: Invalid task name ... The prefix 'task-' is reserved` | 0.42 | [Pass a valid name](#0-42-lock-names) |
| A dashboard on `grelmicro.lock.name` lost a task defined in a script run directly | 0.42 | [Filter on the new lock name](#0-42-lock-names) |
| `AttributeError: 'RedisProvider' object has no attribute 'lock'`, or `'cache'`, `'leaderelection'` and the other pattern names | 0.42 | [Call `lock_backend()`](#0-42-backend-names) |
| `AttributeError: 'Coordination' object has no attribute 'rwlock_backend'` or `'election_backend'` | 0.42 | [Read `readwritelock_backend`](#0-42-backend-names) |
| `TypeError: Coordination.__init__() got an unexpected keyword argument 'rwlock'` or `'election'` | 0.42 | [Pass `readwritelock=` or `leaderelection=`](#0-42-backend-names) |
| `TypeError: Rename MyProvider.lock() to lock_backend()` | 0.42 | [Rename it `lock_backend()`](#0-42-backend-names) |
| `ImportError: cannot import name 'document_idempotency'`, or `'document_conditional_requests'`, `'document_rate_limited_requests'` and `'document_authenticated_requests'` | 0.42 | [Register the component](#0-42-document-functions) |

## 0.42

### A stored duration is whole seconds or a `timedelta` {#0-42-durations}

A lease, a TTL, a task schedule and a circuit breaker reset timeout
take whole seconds as an `int`, or a `timedelta`. A float is refused,
`60.0` included. Each config reads the value back as a `timedelta`.

```python
from datetime import timedelta

from grelmicro.coordination import Lock

# Before
tasks.every(seconds=0.5)
tasks.cron("0 * * * *", gate="claim", misfire_grace_seconds=600)
Lock("cart", lease_duration=0.5)

# After
tasks.every(interval=timedelta(milliseconds=500))
tasks.cron("0 * * * *", gate="claim", misfire_grace=600)
Lock("cart", lease_duration=timedelta(milliseconds=500))
```

From an environment variable, a duration reads whole seconds (`"60"`) or an
ISO 8601 duration (`"PT0.5S"`). A decimal such as `"0.5"` is refused. A method
or decorator argument, such as `cached(ttl=...)`, refuses text.

What moved:

- **Task**: `every(seconds=...)` is now `every(interval=...)`, and cron
  `misfire_grace_seconds` is now `misfire_grace`. The old names are gone.
- **Coordination**: `lease_duration` on `Lock`, `ReadWriteLock`,
  `LeaderElection` and `TaskLock`, `renew_deadline` on `LeaderElection`, and
  `min_hold_duration` on `TaskLock`.
- **Cache**: `TTLCacheConfig.ttl`, `Cache.ttl`, `cached(ttl, stale_ttl)`, and
  the `ttl` and `stale_ttl` of `TTLCache.set`, `.get_or_set` and `.set_many`.
- **HTTP**: `IdempotencyConfig.ttl`, `CachedResponsesConfig.ttl` and its
  per-path `include` TTLs, and `CachedResponse(ttl)`. `RouteDeclaration.cache`
  takes `True` for the component TTL or a `timedelta`. A number is refused.
- **Rate limiter**: the sliding window `window`.
- **Circuit breaker**: `reset_timeout` on `CircuitBreaker.consecutive_count`
  and `ConsecutiveCountConfig`.
- **Security**: `ClientBans` `window` and `duration`, `JWTVerifier` `ttl` and `cache_ttl` (`0` still turns the cache off), and `OAuthClient` `refresh_before` and `default_lifetime`.
- **Outbox**: `lease_duration`, `keep_delivered` and `purge(older_than=...)`.
  `keep_delivered` takes no bool. Write `0` for `False` (delete on delivery)
  and `None` for `True` (keep for good). `keep_delivered=1` now means one
  second, where it used to become `True` and keep rows for good. From an
  environment variable, write `0` or `none`. Bool spellings such as `"true"`
  are refused, and `"1"` means one second too. `purge(older_than=0)` purges
  every delivered and dead row.
- **Log and health**: `DuplicateFilter` `ttl`, and `HealthChecks` `cache_ttl` (`0` still turns the cache off).

A backend of your own takes each lease, TTL or cool-down as a `timedelta`: the
`LockBackend`, `ReadWriteLockBackend`, `LeaderElectionBackend` and
`CacheBackend` protocols, `CircuitBreakerStrategy.transition(cool_down)`, and
`LeaderRecord.lease_duration`. An `OutboxBackend` takes `claim(lease=...)` as
a `timedelta`, and `purge` takes `older_than` as a `timedelta` instead of
`before_seconds`.

Redis leader election stores its record under new `le_us:` keys, so a leader
on the previous version is not seen. Upgrade every worker at once.

### One `gate=` for which workers run a task {#0-42-task-gate}

`every` and `cron` take `gate=` instead of `lock=` and `leader=`, and both run on
every worker by default. A cron task used to claim each fire whenever a
`Coordination` component was wired. Pass `gate="claim"` to keep that:

```python
# Before
@tasks.every(seconds=3600, lock=TaskLock(lease_duration=7200))
@tasks.every(seconds=60, leader=election)
@tasks.cron("0 3 * * *")

# After
@tasks.every(interval=3600, gate="claim")
@tasks.every(interval=60, gate=election)
@tasks.cron("0 3 * * *", gate="claim")
```

`gate="claim"` holds the claim for the whole interval, which the old
`lock=TaskLock(...)` did not: its one-second hold let replicas with offset
timers each run their own tick. A `TaskLock` you still pass as the gate needs a
`min_hold_duration` of at least `interval`.

A gated task with no backend reports a coordination error on every fire
instead of running on every worker. Register a `Coordination` component, or
drop the gate when every worker should run it.

### Every resilience pattern takes `when=` {#0-42-when}

`CircuitBreaker` and `Shield` name the errors they react to with `when=`, like
`Retry` and `Fallback`. It takes an exception class, a tuple of classes, a
predicate or a `Match`.

A breaker's `when=` names the errors that count as failures, and every
`Exception` counts by default. An error you ignored becomes an exclusion:

```python
# Before
CircuitBreaker.consecutive_count("payments", ignore_exceptions=ValidationError)

# After
CircuitBreaker.consecutive_count(
    "payments", when=Match.not_exception(ValidationError)
)
```

A Shield's `when=` names the errors that count as transient, and it is
required. `TimeoutError` still always counts:

```python
# Before
@shield.api(timeout_errors=(httpx.TimeoutException,))
async def fetch(url: str) -> bytes: ...


# After
@shield.api(when=httpx.TimeoutException)
async def fetch(url: str) -> bytes: ...
```

The bare `@shield` is gone, because it had no way to take `when=`. Name a
preset and the errors instead:

```python
# Before
@shield
async def ping() -> None: ...


# After
@shield.api(when=TimeoutError)
async def ping() -> None: ...
```

The same rename applies to `ConsecutiveCountConfig`, the Shield profile
configs and the environment: `GREL_CIRCUITBREAKER_{NAME}_IGNORE_EXCEPTIONS`
and `GREL_SHIELD_{NAME}_TIMEOUT_ERRORS` become `GREL_CIRCUITBREAKER_{NAME}_WHEN`
and `GREL_SHIELD_{NAME}_WHEN`. An ignore list in the environment cannot be
written as an exclusion, so list the errors that count as failures instead.

### The environment no longer picks a Shield preset {#0-42-shield-profile-env}

`GREL_SHIELD_{NAME}_PROFILE` and `GREL_SHIELD_PROFILE` are no longer read. With
environment reads on, a Shield refuses to build while either is set, so a preset
never changes in silence. The environment tunes a Shield's values, and code
chooses its preset. A bare `Shield(...)` builds `api`. Remove the variable and
name the preset it used to pick:

```python
# Before: GREL_SHIELD_DB_PROFILE=internal
db = Shield("db", when=TimeoutError)

# After
db = Shield.internal("db", when=TimeoutError)
```

The decorators follow the same rule: write `@shield.internal(...)` or
`@shield.slow(...)`. `GREL_SHIELD_{NAME}_WHEN` and
`GREL_SHIELD_{NAME}_MAX_RATE` still tune the preset code chose. A `PROFILE` key
in a mounted ConfigMap or Secret is refused the same way: the Shield keeps its
running config and a warning names the key.

### `metrics_router` moved to `grelmicro.integrations.fastapi` {#0-42-metrics-router-moved}

`metrics_router` builds a FastAPI router, so it now sits next to `health_router`:

```python
# Before
from grelmicro.metrics import metrics_router

# After
from grelmicro.integrations.fastapi import metrics_router
```

`metrics_asgi` stays in `grelmicro.metrics`, since it needs no framework.

### The `document_*` functions are removed {#0-42-document-functions}

`document_idempotency`, `document_conditional_requests`, `document_rate_limited_requests` and `document_authenticated_requests` are gone from `grelmicro.integrations.fastapi`. A registered component needs nothing: `micro.install(app)` describes it in the OpenAPI schema on FastAPI and Litestar.

A middleware you added by hand and documented with one of them is registered as its component instead, with the same options:

```python
# Before
app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))
document_idempotency(app)

# After
micro = Grelmicro(uses=[cache, IdempotentRequests()])
micro.install(app)
```

### `CacheError` is removed {#0-42-cache-error}

Nothing raised `CacheError`, so an `except CacheError:` block never ran. Delete it. A backend failure reaches the caller as the backend's own error, such as `redis.exceptions.ConnectionError`.

### `lock=` names how far misses fold {#0-42-fold-scope}

`@cached` and `TTLCache.get_or_set` take the same `lock=`, a backend scope:

| Before | After |
|---|---|
| `@cached(cache, lock="local")` | `@cached(cache, lock="process")`, the default |
| `@cached(cache, lock=True)` | `@cached(cache, lock="cluster")` |
| `@cached(cache, lock=False)` | `@cached(cache, lock=None)` |
| `cache.get_or_set(key, factory)` | `cache.get_or_set(key, factory, lock="cluster")` to keep folding across replicas |

`"host"` and `"cluster"` fold through the lock backend of the app's `Coordination`. Register one, or the call raises `OutOfContextError`. A lock backend that reaches less far than the scope is refused in `staging` and `production`, like `requires=`.

### `max_wait=` bounds every wait to get in {#0-42-max-wait}

Idempotency spells its wait the way the bulkhead and the rate limiter do:

| Before | After |
|---|---|
| `Idempotency(..., wait_timeout=5)` | `Idempotency(..., max_wait=5)` |
| `idem.run(key, fn, wait_timeout=5)` | `idem.run(key, fn, max_wait=5)` |
| `IdempotentRequests(wait_timeout=5)` | `IdempotentRequests(max_wait=5)` |
| `GREL_IDEMPOTENT_REQUESTS_WAIT_TIMEOUT` | `GREL_IDEMPOTENT_REQUESTS_MAX_WAIT` |
| `BulkheadConfig(max_wait=None)` | `BulkheadConfig(max_wait=0)`, the default |

A lock keeps `timeout=` on `acquire()` and `hold()`.

### Idempotency checks its lock, and `CachedResponses` names its fold {#0-42-idempotency-lock}

A duplicate request waits on a lock so it runs once. Without a lock backend that lock holds in one process only, and a duplicate on another replica runs again. The backend check now says so, like it does for the cache. Register a lock backend, or say one replica is all you run:

```python
micro = Grelmicro(uses=[Cache(redis), Coordination(redis), IdempotentRequests()])
# or
micro = Grelmicro(uses=[Cache(redis), IdempotentRequests(requires="process")])
```

`CachedResponses` folds concurrent misses in the process by default, like `@cached`. Pass `CachedResponses(lock="cluster")` to fold them through the lock backend, as it did whenever one existed.

### Extend a lease with `extend()` {#0-42-lock-extend}

`TaskLock.refresh()` is now `TaskLock.extend()`, like `Lock` and
`ReadWriteLock`:

```python title="fragment"
# Before
await task_lock.refresh()

# After
await task_lock.extend()
```

A backend failure while extending a lease raises `LockExtendError`, on every
lock and when a task gate extends its claim. `Lock` and `ReadWriteLock` raised
`LockAcquireError` there, and `TaskLock` raised `LockReleaseError`.
`except LockBackendError` catches every backend failure.

A lost lease still raises `LockNotOwnedError`, which is no longer a
`LockReleaseError` or a `LockBackendError`. Code that used
`except LockReleaseError` to also catch a lost lease adds
`except LockNotOwnedError`.

The `grelmicro.lock.renewals` metric is now `grelmicro.lock.extensions`.
Switch dashboards and alerts on the old name to the new one.

### `key=` is always a function {#0-42-key-function}

`key=` now takes the function deriving the key, everywhere. `key_maker=` is
gone. `@cached` and `@limiter` take a template string as `key_template=`, and a
string passed to `key=` raises `TypeError`:

```python
# Before
@cached(cache, key="user:{user_id}")
@cached(cache, key_maker=lambda func, args, kwargs: f"user:{args[0]}")
@limiter(key="user:{user_id}")
@limiter(key_maker=lambda func, args, kwargs: f"user:{args[0]}")
IdempotentRequests(key_maker=tenant_key)

# After
@cached(cache, key_template="user:{user_id}")
@cached(cache, key=lambda func, args, kwargs: f"user:{args[0]}")
@limiter(key_template="user:{user_id}")
@limiter(key=lambda func, args, kwargs: f"user:{args[0]}")
IdempotentRequests(key=tenant_key)
```

`IdempotencyMiddleware(key_maker=...)` is `IdempotencyMiddleware(key=...)` the
same way. `IdempotencyKeyMakerError` is renamed `IdempotencyKeyFunctionError`.

### `from_config` takes the constructor's arguments in its order {#0-42-from-config}

`AuthenticatedRequests.from_config` takes the verifier first, like
`AuthenticatedRequests(verifier)`:

```python title="fragment"
# Before
AuthenticatedRequests.from_config(config, verifier)

# After
AuthenticatedRequests.from_config(verifier, config)
```

### A `TaskLock` needs a name {#0-42-lock-names}

`TaskLock()` has no name by default. As the `gate` of a task it takes the task
name, as before. Used on its own, it raises `SettingsValidationError` when
entered. Pass the name it used to share:

```python title="fragment"
# Before
task_lock = TaskLock()

# After
task_lock = TaskLock("cleanup")
```

A gate named `TaskLock("default")` keeps that name instead of taking the task
name. Drop the name to have it take the task name.

`TaskLock`, `LeaderElection` and a gated task's `name=` follow the `Lock` rule:
a letter or digit, then letters, digits and `._:/-`, up to 200 characters.
Rename one that does not, such as `name="daily report"` to
`name="daily-report"`. A gated task's `name=` cannot start with `task-` either.

A task named after its function, whose name is not a valid lock name, locks
under a derived name. In a script run directly, `__main__:job` locks under
`task-__main__:job`. That is also its gate log label and its
`grelmicro.lock.name` metric label, so update dashboards that filter on the
old one. Workers on the previous version lock it under the old key and do not
block the new ones, so upgrade every worker running such a task at once.

### Provider methods end in `_backend` {#0-42-backend-names}

A provider method that builds a backend now says so. `Coordination` takes and
exposes its backends under the same names:

| Before | After |
|---|---|
| `provider.lock()` | `provider.lock_backend()` |
| `provider.readwritelock()` | `provider.readwritelock_backend()` |
| `provider.leaderelection()` | `provider.leaderelection_backend()` |
| `provider.schedule()` | `provider.schedule_backend()` |
| `provider.cache()` | `provider.cache_backend()` |
| `provider.outbox()` | `provider.outbox_backend()` |
| `provider.ratelimiter()` | `provider.ratelimiter_backend()` |
| `provider.circuitbreaker()` | `provider.circuitbreaker_backend()` |
| `coordination.rwlock_backend` | `coordination.readwritelock_backend` |
| `coordination.election_backend` | `coordination.leaderelection_backend` |
| `Coordination(rwlock=...)` | `Coordination(readwritelock=...)` |
| `Coordination(election=...)` | `Coordination(leaderelection=...)` |
| `grelmicro.coordination.election.adapters` | `grelmicro.coordination.leaderelection.adapters` |

```python title="fragment"
# Before
leader = LeaderElection("worker", backend=redis.leaderelection())

# After
leader = LeaderElection("worker", backend=redis.leaderelection_backend())
```

A custom `Provider` renames the methods it overrides the same way, such as
`def lock_backend(self, **kwargs)`. A subclass that still defines `lock()`
without `lock_backend()` fails when the class is defined, naming the rename.
Component methods are unchanged: `micro.coordination.lock("cart")` still
returns a `Lock`, and `coordination.lock_backend` and
`coordination.schedule_backend` keep their names.

A plugin that ships a leader election adapter registers it under the
`grelmicro.coordination.leaderelection.adapters` entry-point group.

## 0.40

### One error for every bad configuration value {#0-40-one-settings-error}

A bad configuration value raises `SettingsValidationError`, whichever pattern
or component you built. The ten per-module subclasses are gone:
`CacheSettingsValidationError`, `CoordinationSettingsValidationError`,
`HealthSettingsValidationError`, `IdempotencySettingsValidationError`,
`LogSettingsValidationError`, `MetricsSettingsValidationError`,
`OutboxSettingsValidationError`, `ResilienceSettingsValidationError`,
`TaskSettingsValidationError`, and `TraceSettingsValidationError`.

Catch the base error, which every one of them already subclassed:

```python
# Before
from grelmicro.log import Log, LogSettingsValidationError

try:
    Log(level="NOPE")
except LogSettingsValidationError:
    ...

# After
from grelmicro import SettingsValidationError

try:
    Log(level="NOPE")
except SettingsValidationError:
    ...
```

`except ValueError` and `except GrelmicroError` keep working unchanged.

The same applies to `cached()`, `TrustedProxies`, and `ExternalConfig`, which
raised a bare `ValueError` or `TypeError`. `cached(ttl=-1)` already raised
`SettingsValidationError` while `lock=`, `early=`, and `stale_ttl=` did not, so
one call had two contracts.

A refused *name* moved the same way: a bad `Lock` name, an adapter
`table_name`, or a Redis `prefix` that cannot survive a cluster. The name is
still repeated in the message, since it is a literal you wrote in code rather
than a value read from a variable.

`Fallback`, `Shield`, and `TTLCache` used to let pydantic's `ValidationError`
through instead, so they now raise `SettingsValidationError` too. If you catch
`ValidationError` around one of those, catch `SettingsValidationError`. It
subclasses `ValueError`, which `ValidationError` also is, so an
`except ValueError` around either keeps working.

That change closes a leak: pydantic attaches the rejected input to its error,
so an invalid value read from the environment used to reach the traceback.
Errors now carry the variable name and the reason, never the value.

### An unknown environment is refused {#0-40-environment-validated}

`Grelmicro(environment=...)` stored whatever it was given. A value outside the
four tiers was accepted in silence, and the backend check then ran as if no tier
had been declared, which is the check that refuses a memory backend in
production. It now raises:

```python
# Before: accepted, and the backend check quietly went soft
Grelmicro(environment="prod")

# After
Grelmicro(environment="production")
```

The four tiers are `development`, `test`, `staging`, and `production`.
`GREL_ENVIRONMENT` already warned on an unknown value, so this makes the two
doors agree.

### A bad `Match` argument raises `ValueError` {#0-40-match-value-error}

`Match.exception()` and `Match.exception_cause()` raised `TypeError` when an
argument was not an exception class, and when they got no argument at all.
`Match.exception_message()` raised `TypeError` when it got both `contains=`
and `regex=`, or neither. `when=` raised `TypeError` for a value that was not
a `Match`, an exception class, a tuple of them, or a callable. They raise
`ValueError` now, and so do `Match.predicate()` for a non-callable and
`Match.exception_message()` for a `contains=` that is not a string or a
`regex=` that is neither a string nor a compiled pattern:

```python
# Before
try:
    Match.exception(ValueError, "nope")
except TypeError:
    ...

# After
try:
    Match.exception(ValueError, "nope")
except ValueError:
    ...
```

pydantic converts only `ValueError` and `AssertionError` into a validation
error, so the old `TypeError` escaped `except SettingsValidationError` and
`except ValueError` alike when the same value arrived through
`GREL_RETRY_{NAME}_WHEN`. One empty entry in a mounted ConfigMap was enough to
abort a whole reload cycle.

### The provider error subclasses are gone {#0-40-provider-errors}

`RedisProviderConfigError` and `PostgresProviderConfigError` are removed. A
provider raises `SettingsValidationError`, like every other class:

```python
# Before
from grelmicro.providers.redis import RedisProviderConfigError

try:
    RedisProvider("anything://localhost:6379")
except RedisProviderConfigError:
    ...

# After
from grelmicro import SettingsValidationError

try:
    RedisProvider("anything://localhost:6379")
except SettingsValidationError:
    ...
```

They were the last two per-module subclasses, left behind when the other ten
went. `except ValueError` and `except GrelmicroError` keep working unchanged.

## 0.39

### `grelmicro.clientip` moved to `grelmicro.security` {#0-39-clientip-moved}

Client IP resolution is one of the checks a service runs on an inbound
request, so it now lives with them. The names and their behaviour are
unchanged. Rename the import:

```python
# Before
from grelmicro.clientip import TrustedProxies, resolve_client_address

# After
from grelmicro.security import TrustedProxies, resolve_client_address
```

The logger moved with it, from `grelmicro.clientip` to
`grelmicro.security.clientip`. A filter, a level, or a handler attached to the
old name stops matching, and silently, because nothing logs on that name any
more. Rename it wherever your logging config names it.

## 0.37.1

### A provider URL is validated on every path {#0-37-1-url-validation}

A URL passed to a provider constructor is now checked against the same type
as one read from the environment. Both paths accept the same URLs and raise
the same error, so a URL the client library used to accept can now be refused
where it used to connect:

```python
# Before: redis-py accepted the authority and connected to h1 alone
RedisProvider("redis+sentinel://h1:26379,/mymaster")

# After
# SettingsValidationError: Could not validate settings:
# - url: Input should be a valid URL, empty host
```

The message names what is wrong with the URL. Fix the URL, most often a
trailing comma in a multi-host authority, a missing host, or a port that is
not a number.

Nothing else moves: `redis://`, `rediss://`, `unix://`, `redis+sentinel://`,
`redis+cluster://`, the four `valkey` spellings, and every Postgres scheme
including the SQLAlchemy driver forms are all accepted as before.

## 0.37

### `Registry` classes are now `Component` {#0-37-registry-renamed}

`RateLimiterRegistry` is `RateLimiterComponent` and `CircuitBreakerRegistry`
is `CircuitBreakerComponent`. Neither ever registered anything: each wraps
one backend. Rename the import and the call:

```python
# Before
from grelmicro import Grelmicro
from grelmicro.resilience import CircuitBreakerRegistry, RateLimiterRegistry

micro = Grelmicro(
    uses=[RateLimiterRegistry(redis), CircuitBreakerRegistry(redis)]
)


# After
from grelmicro.resilience import CircuitBreakerComponent, RateLimiterComponent

micro = Grelmicro(
    uses=[RateLimiterComponent(redis), CircuitBreakerComponent(redis)]
)
```

Most wiring needs neither name. `Grelmicro(uses=[redis])` registers a
component for every kind the provider serves, so name the class only for a
second instance or for `micro.override(...)`.

`health_router(registry=...)` is now `health_router(component=...)`, matching
`metrics_router(component=...)`:

```python
# Before
app.include_router(health_router(registry=health))

# After
app.include_router(health_router(component=health))
```

## 0.36

### `LogTimeZoneType` is gone {#0-36-timezone-type}

Use [`TimeZoneName`](reference/types.md) from `grelmicro.types`, which every
component that takes a timezone now shares:

```python
# Before
from grelmicro.log import LogTimeZoneType

# After
from grelmicro.types import TimeZoneName
```

### A timezone abbreviation no longer validates {#0-36-timezone-abbreviation}

`GREL_LOG_TIMEZONE=CEST` used to validate and then fail later, because
`zoneinfo` has no such zone. It now raises `SettingsValidationError:
unknown timezone name` where the value is read. The message does not repeat
the value, so the variable name is what locates it. Abbreviations such as
`CEST`, `PST`, `PDT`, `EDT`, `BST`, and `JST` are daylight saving variants,
not zones, and pinning one would freeze the offset year-round. Name the zone
instead:

```bash
# Before
GREL_LOG_TIMEZONE=CEST

# After
GREL_LOG_TIMEZONE=Europe/Zurich
```

Real zone names that look like abbreviations keep working, starting with the
default `UTC`, and including `CET`, `EET`, `GMT`, `EST`, `MST`, and `HST`.

## 0.34

### `@cached` on a method needs an explicit key {#0-34-cached-method-key}

Decorating a method without `key=` or `key_maker=` now raises `TypeError` at
decoration time, so the failure lands at import rather than on a request.

The default key is the `repr()` of every argument, and on a method the first
one is `self`. That read two ways, both wrong. Two instances whose `repr()`
matched shared one entry, so a call on one returned the other's value. An
instance using the default `repr()` carried a memory address, so its key
changed on every restart and the entry was never found again.

Name what identifies the entry and leave `self` out:

```python
# Before
class Repo:
    @cached(cache)
    async def load(self, user_id: int) -> User: ...


# After
class Repo:
    @cached(cache, key="repo:{user_id}")
    async def load(self, user_id: int) -> User: ...
```

When the result does depend on instance state, fold that state into the key:

```python
@cached(cache, key="repo:{self.region}:{user_id}")
async def load(self, user_id: int) -> User: ...
```

A `staticmethod` and a `classmethod` are untouched. Neither receives an
instance, so their default key was already sound.

Entries written before the upgrade are not reachable under the new key, so
expect one cold period for the functions you change.

## 0.32

### SQLite adapters take a provider, not a path {#0-32-sqlite-provider}

`SQLiteLockAdapter` and `SQLiteScheduleAdapter` now take `provider=`, like
every other SQLite adapter.

```python
# Before
SQLiteLockAdapter("app.db")

# After
SQLiteLockAdapter(provider=SQLiteProvider("app.db"))
```

Better still, pass the provider to the component and let it build both:

```python
sqlite = SQLiteProvider("app.db")
micro = Grelmicro(uses=[Coordination(sqlite)])
```

That also shares one connection across every component on the same file,
where the old form opened its own.

A missing path raises `SettingsValidationError`, as every configuration
failure does since 0.40.

### URL and header fields hide their credentials {#0-32-secret-urls}

`url` on `PostgresConfig` and `RedisConfig`, and `endpoint` on `TraceConfig`
and `MetricsConfig`, are now `SecretUrl`. Each `headers` value on
`TraceConfig` and `MetricsConfig` is now a `SecretStr`.

Passing a plain string still works. Only reading the value back changes:

```python
# Before
dsn = config.url

# After
dsn = config.url.get_secret_value()
```

Nothing changes on the wire. The point is that `repr()`, `model_dump()` and
a `ValidationError` no longer carry the password.

## 0.31

### Credential fields hide their value {#0-31-secret-credentials}

`basic_auth_password` on `TraceConfig` and `MetricsConfig`, and `password` on
`PostgresConfig` and `RedisConfig`, are now `SecretStr`. Same shape as the
0.32 change above: passing a plain string still works, reading it back needs
`.get_secret_value()`.

### `Metrics()` no longer defaults to localhost {#0-31-metrics-auto-exporter}

`Metrics()` now defaults to the `auto` exporter. With an endpoint configured
it exports over OTLP HTTP. Without one it auto-disables into a true no-op,
where it previously fell back to `localhost:4318`.

**This one fails quietly.** If you relied on the implicit localhost default,
metrics simply stop arriving and nothing raises. Set the endpoint
explicitly:

```python
Metrics(endpoint="http://localhost:4318")
```

Or from the environment, `GREL_METRICS_ENDPOINT`.

The upside is that you can now register `Metrics()` unconditionally: an
auto-disabled `Metrics` installs no provider and never conflicts with a
second app.

## 0.30

### `Operation.response` became `result()` {#0-30-operation-result}

The idempotency `Operation.response` attribute is now a `result()` method,
typed as the stored type so a replay branch returns it without a cast.

```python title="fragment"
async with idem(key) as op:
    if op.replayed:
        return op.result()  # was: op.response
    ...
```

It is valid **only** on a replay. Calling it on a first execution raises
`IdempotencyStateError`, so keep it behind `if op.replayed:`.

## 0.41, not breaking but worth knowing {#0-41-async-callable-objects}

Every decorator now reads an object whose `__call__` is async as async.
Before, `inspect.iscoroutinefunction` reported `False` for one, so the
decorator built its sync wrapper. That wrapper called the object, took
the coroutine it returned as the result, and returned. The body ran
later, when your code awaited that coroutine, outside the policy.

```python
class Client:
    async def __call__(self, order_id: str) -> Order:
        ...


fetch = retry(when=ConnectionError, attempts=3)(Client())
```

The retry above made **one** attempt, not three, and the first failure
reached the caller with no backoff and no budget.

Each decorator showed it differently, which is why the symptom table
above lists three rows:

- `@retry` and `@fallback` returned without engaging, silently. `@measure`
  and `@instrument` timed the creation of the coroutine rather than the
  call, so a slow body recorded almost no time and a failing one recorded
  no error.
- `@timeout`, `@bulkhead` and `@shield` refused the object at decoration,
  with a `TypeError` saying they only decorate async functions.
- `@cached(ttl=...)` refused it too, with its own wording: `supports
  async functions only`. `@cached` on a `TTLCache` you passed did not
  refuse, and raised `AttributeError: '...' object has no attribute
  '__qualname__'` instead.
- `CircuitBreaker` raised `EventLoopDeadlockError`, because the sync path
  it took is the one meant for a worker thread and it saw the event loop
  on the other side. That error is a `BaseException`, so an
  `except Exception` around the call did not catch it.

Nothing in your code has to change. A decorator applied to a plain
async function, which is how nearly everyone writes this, was never
affected.

The behaviour of a running deployment changes only where the failure was
silent, because the loud ones stopped the app from starting at all.
So look at `@retry`, `@fallback`, `@measure` and `@instrument` on a
callable object: a retry that never fired starts firing, and a call that
recorded no time starts recording it.

## 0.34, not breaking but worth knowing {#0-34-task-run-outcomes}

`grelmicro.task.runs` now counts every fire, not only the fires that ran
the body. A fire a peer took counts as `skipped`, a fire dropped past its
grace budget as `missed`, and a fire that never got there because
coordination failed as `coordination_error`.

That makes the bare total larger. On a fleet of N workers sharing a lock
it is now roughly N times the number of fires, because the N-1 workers
that stand down each count their fire.

If a chart or an alert reads the total as "how often does my task run",
filter it:

```promql
# Before
rate(grelmicro_task_runs_total[5m])

# After
rate(grelmicro_task_runs_total{grelmicro_outcome="success"}[5m])
```

The values keep the meanings they had. The label itself was renamed from
`outcome` to `grelmicro_outcome` in a later release, and the query above
already carries the new spelling.

## 0.32.2, not breaking but worth knowing {#0-32-2-circuit-breaker-state}

Circuit breakers now reclaim their stored state instead of keeping it
forever. Rows written before 0.32.2 carry no activity timestamp, so an
already-open circuit on Postgres or SQLite reads as expired the first time
the new code touches it and starts again from `CLOSED`.

This happens once, on the first call after the upgrade. Circuits held open
by `isolate()` are unaffected.

## About deprecation

Before 1.0 a rename is a clean cut: the old name is removed in the same
release the new one appears, with no deprecation cycle and no alias. That
keeps a fast-moving `0.x` from accumulating shims, and it is why this page
exists instead.

After 1.0, `1.x` follows standard semver and breaking changes go through a
deprecation cycle.
