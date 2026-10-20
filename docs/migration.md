# Migration

One section per minor release from 0.30, newest first. Each section opens
with two lists:

- **Changes that do not raise**: your code keeps running, but something
  behaves differently. Nothing warns you, so read this list first.
- **Changes that raise**: an import, a constructor, the startup or the first
  call fails, and the error names what to change.

The detailed entries follow, with a Before and an After where code changes.
Each section ends with a link to its full changelog. Seeing an error or an
odd metric already? Match it in the table below.

For a version before 0.30, read the `Breaking` sections in the
[changelog](changelog.md), newest first.

In `0.x` the minor is the breaking position: a `0.x.0` release may change the
public API, and a `0.x.y` release is a safe patch. So an upgrade within one
minor rarely needs this page. A few patches are exceptions, and each one sits in
its minor's section: [0.32.2](#0-32-2-circuit-breaker-state) closes an open
circuit once, [0.35.1](#0-35-1-uvicorn-format) reformats uvicorn's lines and
warns on an ignored variable, [0.37.1](#0-37-1-url-validation) refuses a
provider URL that a client library used to accept, and 0.42.1 [renames Starlette
mount routes](#0-42-silent) and [moves when an ambiguous provider
raises](#0-42-raising).

## Find your symptom

| Symptom | Release | Fix |
|---|---|---|
| `AttributeError: 'Operation' object has no attribute 'response'` | 0.30 | [Use `result()`](#0-30-operation-result) |
| A password or URL reads back as `SecretStr(...)` or `***` | 0.31, 0.32 | [Call `.get_secret_value()`](#0-31-secret-credentials) |
| Metrics stopped arriving after upgrading, with no error | 0.31 | [Set an endpoint](#0-31-metrics-auto-exporter) |
| `TypeError` on a SQLite adapter, either `unexpected keyword argument 'path'` or `takes 1 positional argument` | 0.32 | [Pass `provider=`](#0-32-sqlite-provider) |
| An open circuit closed once, just after upgrading | 0.32.2 | [Nothing to change](#0-32-2-circuit-breaker-state) |
| `TypeError: @cached on ... needs an explicit key=` | 0.34 | [Name the key](#0-34-cached-method-key) |
| Task run totals jumped after upgrading, with no new failures | 0.34 | [Filter on the outcome](#0-34-task-run-outcomes) |
| A `/healthz` consumer fails on a missing `error` field | 0.35 | [Read an absent `error` as a pass](#0-35-healthz-fields) |
| A log pipeline stopped parsing uvicorn's lines | 0.35.1 | [Update the parser or turn it off](#0-35-1-uvicorn-format) |
| `GrelmicroConfigWarning` fails a test run under `-W error` | 0.35.1 | [Fix the configuration](#0-35-1-config-warning) |
| `ImportError: cannot import name 'LogTimeZoneType'` | 0.36 | [Use `TimeZoneName`](#0-36-timezone-type) |
| `SettingsValidationError: unknown timezone name` on a value like `CEST` | 0.36 | [Name the zone](#0-36-timezone-abbreviation) |
| `ImportError: cannot import name 'RateLimiterRegistry'` or `ImportError: cannot import name 'CircuitBreakerRegistry'` | 0.37 | [Rename to `Component`](#0-37-registry-renamed) |
| `TypeError: health_router() got an unexpected keyword argument 'registry'` | 0.37 | [Pass `component=`](#0-37-registry-renamed) |
| A provider error on a URL that used to connect | 0.37.1 | [Fix the URL](#0-37-1-url-validation) |
| A bare `GREL_{PATTERN}_` variable changed every instance, not only the default one | 0.38 | [Name the default instance](#0-38-bare-prefix) |
| `ModuleNotFoundError: No module named 'grelmicro.clientip'` | 0.39 | [Import from `grelmicro.security`](#0-39-clientip-moved) |
| A logging filter or level set on `grelmicro.clientip` stopped matching | 0.39 | [Rename the logger](#0-39-clientip-moved) |
| `ImportError: cannot import name 'LogSettingsValidationError'`, or any other `*SettingsValidationError` | 0.40 | [Catch the base error](#0-40-one-settings-error) |
| `SettingsValidationError` where you caught `pydantic.ValidationError` from `Fallback`, `Shield`, or `TTLCache` | 0.40 | [Catch the base error](#0-40-one-settings-error) |
| `SettingsValidationError` where you caught `ValueError` or `TypeError` from `cached()`, `TrustedProxies`, or `ExternalConfig` | 0.40 | [Catch the base error](#0-40-one-settings-error) |
| `SettingsValidationError` on a bad `Lock` name, adapter `table_name`, or Redis `prefix` | 0.40 | [Catch the base error](#0-40-one-settings-error) |
| `SettingsValidationError: environment= must be one of ...` on a `Grelmicro(...)` that used to build | 0.40 | [Name a real tier](#0-40-environment-validated) |
| `ValueError` where you caught `TypeError` from any `Match` argument error or a bad `when=` | 0.40 | [Catch `ValueError`](#0-40-match-value-error) |
| `ImportError: cannot import name 'RedisProviderConfigError'` or `'PostgresProviderConfigError'` | 0.40 | [Catch the base error](#0-40-provider-errors) |
| A dashboard or alert on a grelmicro metric label, such as `outcome` or `task_name`, shows no data | 0.41 | [Update the queries](#0-41-metric-names) |
| A dashboard on `grelmicro.bulkhead.rejections` or `grelmicro.rate_limiter.decisions` stopped receiving data | 0.41 | [Update the queries](#0-41-metric-names) |
| `grelmicro.retry.attempts` jumped after upgrading, with no new failures | 0.41 | [Update the queries](#0-41-metric-names) |
| `/livez`, `/readyz`, `/healthz` or `/metrics` is missing from the OpenAPI schema | 0.41 | [Pass `include_in_schema=True`](#0-41-health-openapi) |
| A JSON log field reads differently, such as a `UUID`, an `Enum` or non-ASCII text | 0.41 | [Pin the serializer](#0-41-json-serializer) |
| An idempotent request with `Authorization` or `Cookie` runs again instead of replaying | 0.41 | [Pass a key function](#0-41-idempotency-bypass) |
| A request retried across the upgrade ran twice | 0.41 | [Nothing to change](#0-41-idempotency-bypass) |
| Uvicorn's startup and error lines left stderr | 0.41 | [Turn the pass off](#0-41-uvicorn-stream) |
| A `@retry` or `@fallback` on a callable object never engaged, or `@measure` recorded almost no time for it | 0.41 | [Nothing to change](#0-41-async-callable-objects) |
| `TypeError: ... only decorates async functions` from `@timeout`, `@bulkhead` or `@shield` on a callable object | 0.41 | [Nothing to change](#0-41-async-callable-objects) |
| `TypeError: @cached(ttl=...) supports async functions only`, or `AttributeError: '...' object has no attribute '__qualname__'` from `@cached` | 0.41 | [Nothing to change](#0-41-async-callable-objects) |
| `EventLoopDeadlockError` from a `CircuitBreaker` on a callable object, which an `except Exception` did not catch | 0.41 | [Nothing to change](#0-41-async-callable-objects) |
| `TypeError: ... got an unexpected keyword argument 'lock'` or `'leader'` from `every` | 0.42 | [Pass `gate=`](#0-42-task-gate) |
| A cron task runs on every replica after upgrading | 0.42 | [Pass `gate="claim"`](#0-42-task-gate) |
| Rate limits and lockouts reset after upgrading | 0.42 | [Keep the overlap short](#0-42-rolling-deploy) |
| A caller gets twice its rate limit during a rolling deploy | 0.42 | [Keep the overlap short](#0-42-rolling-deploy) |
| Two leaders, or a task running twice, during a rolling deploy | 0.42 | [Stop every 0.41 worker first](#0-42-rolling-deploy) |
| `SettingsValidationError: lock= takes 'process', 'host', 'cluster' or None` | 0.42 | [Name the fold scope](#0-42-fold-scope) |
| `OutOfContextError` from a `@cached` function or `get_or_set` with `lock="cluster"` | 0.42 | [Register a lock backend](#0-42-fold-scope) |
| A `get_or_set` value computed once per replica after upgrading | 0.42 | [Pass `lock="cluster"`](#0-42-fold-scope) |
| `TypeError: ... got an unexpected keyword argument 'wait_timeout'` | 0.42 | [Pass `max_wait=`](#0-42-max-wait) |
| `ValidationError` from `BulkheadConfig(max_wait=None)`, or `SettingsValidationError` on `GREL_BULKHEAD_{NAME}_MAX_WAIT` set to `none` | 0.42 | [Pass `0`](#0-42-max-wait) |
| `BackendScopeError: ... is bound to InProcessLock` on `Idempotency` or `IdempotentRequests` | 0.42 | [Register a lock backend](#0-42-idempotency-lock) |
| `CachedResponses` misses run once per replica after upgrading | 0.42 | [Pass `lock="cluster"`](#0-42-idempotency-lock) |
| `ImportError: cannot import name 'metrics_router' from 'grelmicro.metrics'` | 0.42 | [Import from `grelmicro.integrations.fastapi`](#0-42-metrics-router-moved) |
| `ModuleNotFoundError: No module named 'grelmicro.metrics.fastapi'` | 0.42 | [Import from `grelmicro.integrations.fastapi`](#0-42-metrics-router-moved) |
| `ImportError: cannot import name 'CacheError' from 'grelmicro.cache'` | 0.42 | [Delete the `except CacheError:` block](#0-42-cache-error) |
| `SettingsValidationError: Could not validate settings: min_hold_duration must be greater than or equal to interval` | 0.42 | [Hold the claim for the interval](#0-42-task-gate) |
| `SettingsValidationError` or `ValueError`: `... must be whole seconds or a timedelta` | 0.42 | [Pass whole seconds or a `timedelta`](#0-42-durations) |
| `TypeError: TaskRouter.every() got an unexpected keyword argument 'seconds'` | 0.42 | [Pass `interval=`](#0-42-durations) |
| `TypeError: ... got an unexpected keyword argument 'misfire_grace_seconds'` | 0.42 | [Pass `misfire_grace=`](#0-42-durations) |
| `TypeError: ... got an unexpected keyword argument 'ignore_exceptions'` from a `CircuitBreaker` | 0.42 | [Pass `when=`](#0-42-when) |
| `TypeError: ... got an unexpected keyword argument 'timeout_errors'` from a `Shield` | 0.42 | [Pass `when=`](#0-42-when) |
| `SettingsValidationError: ... when: Field required` from a `Shield` | 0.42 | [Pass `when=`](#0-42-when) |
| `TypeError: '_ShieldDecorator' object is not callable` from a bare `@shield` | 0.42 | [Pass a preset and `when=`](#0-42-when) |
| `AttributeError: 'TaskLock' object has no attribute 'refresh'` | 0.42 | [Follow the `extend()` rename](#0-42-lock-extend) |
| `LockExtendError` where you caught `LockAcquireError` or `LockReleaseError` from extending a lease | 0.42 | [Catch `LockExtendError`](#0-42-lock-extend) |
| `LockNotOwnedError` passes an `except LockReleaseError` or `except LockBackendError` | 0.42 | [Catch `LockNotOwnedError`](#0-42-lock-extend) |
| A dashboard or alert on `grelmicro.lock.renewals` stopped receiving data | 0.42 | [Follow the `extend()` rename](#0-42-lock-extend) |
| `TypeError: key must be a function, use key_template= for a template` from `@cached` or `@limiter` | 0.42 | [Pass `key_template=`](#0-42-key-function) |
| `TypeError: ... got an unexpected keyword argument 'key_maker'` | 0.42 | [Pass `key=`](#0-42-key-function) |
| `ImportError: cannot import name 'IdempotencyKeyMakerError'` | 0.42 | [Catch `IdempotencyKeyFunctionError`](#0-42-key-function) |
| `SettingsValidationError` naming `GREL_SHIELD_{NAME}_PROFILE` or `GREL_SHIELD_PROFILE`: `is no longer read` | 0.42 | [Choose the preset in code](#0-42-shield-profile-env) |
| `SettingsValidationError: TaskLock has no name` | 0.42 | [Update the lock name](#0-42-lock-names) |
| `SettingsValidationError: Invalid lock name ...` from a `TaskLock`, a `LeaderElection` or a gated task | 0.42 | [Update the lock name](#0-42-lock-names) |
| `SettingsValidationError: Invalid task name ... The prefix 'task-' is reserved` | 0.42 | [Update the lock name](#0-42-lock-names) |
| A dashboard on `grelmicro.lock.name` lost a task defined in a script run directly | 0.42 | [Update the lock name](#0-42-lock-names) |
| `AttributeError: 'RedisProvider' object has no attribute 'lock'`, or `'cache'`, `'leaderelection'` and the other pattern names | 0.42 | [Call `lock_backend()`](#0-42-backend-names) |
| `AttributeError: 'Coordination' object has no attribute 'rwlock_backend'` or `'election_backend'` | 0.42 | [Read `readwritelock_backend`](#0-42-backend-names) |
| `TypeError: Coordination.__init__() got an unexpected keyword argument 'rwlock'` or `'election'` | 0.42 | [Pass `readwritelock=` or `leaderelection=`](#0-42-backend-names) |
| `TypeError: Rename MyProvider.lock() to lock_backend()` | 0.42 | [Rename it `lock_backend()`](#0-42-backend-names) |
| `ImportError: cannot import name 'document_idempotency'`, or `'document_conditional_requests'` and `'document_rate_limited_requests'` | 0.42 | [Register the component](#0-42-document-functions) |
| A URL no route answers is no longer served from the cache or replayed, on an app without `AuthenticatedRequests` | 0.42 | [Declare the route](#0-42-answered-at-route) |
| `404` or `405` where a write without `If-Match` got `428`, on an app without `AuthenticatedRequests` | 0.42 | [Nothing to change](#0-42-answered-at-route) |
| `404` or `405` where a request without `Idempotency-Key` got `400` from `require_key=True`, on an app without `AuthenticatedRequests` | 0.42 | [Nothing to change](#0-42-answered-at-route) |
| A path an `include=` pattern names under middleware on a mount or a mounted app is no longer cached | 0.42 | [Declare the route](#0-42-answered-at-route) |
| A route declaring `CachedResponse()` is no longer cached by a `CachedResponsesMiddleware` added by hand | 0.42 | [Register the component](#0-42-hand-added) |
| A middleware added by hand answers before a route's own checks, such as an authentication dependency | 0.42 | [Register the component](#0-42-hand-added) |
| `TypeError: ... got an unexpected keyword argument 'policies'` from `CachedResponsesMiddleware` | 0.42 | [Drop it](#0-42-hand-added) |
| `GREL_CIRCUITBREAKER_{NAME}_IGNORE_EXCEPTIONS` or `GREL_SHIELD_{NAME}_TIMEOUT_ERRORS` no longer applies, with no error | 0.42 | [Rename the variable to `_WHEN`](#0-42-when) |
| `GREL_IDEMPOTENT_REQUESTS_WAIT_TIMEOUT` no longer applies, with no error | 0.42 | [Rename the variable to `_MAX_WAIT`](#0-42-max-wait) |
| A dashboard on `http.server.duration` stopped receiving data on FastAPI | 0.42 | [Read `http.server.request.duration`, in seconds](changelog.md#0420-2026-10-19) |

## 0.42

### Changes that do not raise {#0-42-silent}

- A `cron` task with no `gate=` runs each fire on every worker. [Pass `gate="claim"`](#0-42-task-gate).
- A gated task with no backend reports a coordination error and runs nothing. [Register a `Coordination`](#0-42-task-gate).
- Sliding window limits and lockouts start empty, and a caller gets up to twice its limit while 0.41 and 0.42 workers overlap. [Keep the overlap short](#0-42-rolling-deploy).
- Redis leader election and a task with a derived lock name do not see a 0.41 worker. [Stop every 0.41 worker first](#0-42-rolling-deploy).
- `keep_delivered=1` keeps outbox rows for one second, not for good. [Write `None`](#0-42-durations).
- `TTLCache.get_or_set` folds concurrent misses in one process, not across replicas. [Pass `lock="cluster"`](#0-42-fold-scope).
- `CachedResponses` folds concurrent misses in one process, not across replicas. [Pass `lock="cluster"`](#0-42-idempotency-lock).
- The response cache, idempotency and conditional requests answer at the route. A URL no route answers is neither cached nor replayed. [Declare the route](#0-42-answered-at-route).
- A cache, idempotency or conditional middleware added by hand reads no route. [Register the component](#0-42-hand-added).
- An integration without the route hooks declares no route. [Ship both hooks](#0-42-route-hooks).
- The `grelmicro.lock.renewals` metric is now `grelmicro.lock.extensions`. [Follow the `extend()` rename](#0-42-lock-extend).
- A task named after a function whose name is not a valid lock name locks under a `task-` name, which is also its metric label. [Update the lock name](#0-42-lock-names).
- The default idempotency key follows the checks each route declares. More routes stop replaying across callers, and a FastAPI route with only grelmicro's own dependencies now replays. [Pass a key function](changelog.md#0420-2026-10-19).
- A Litestar handler with a guard, a dependency, a hook or middleware is never cached. [Nothing to change](changelog.md#0420-2026-10-19).
- On FastAPI, `http.server.duration` in milliseconds becomes `http.server.request.duration` in seconds, and the body size metrics are gone. FastAPI records the request spans and metrics itself. [Read `http.server.request.duration`, in seconds](changelog.md#0420-2026-10-19).
- On FastAPI, `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*` and `OTEL_SEMCONV_STABILITY_OPT_IN` no longer apply to requests. [Remove the variables](changelog.md#0420-2026-10-19).
- An access log added by hand names no route. A request answered before routing, such as a cached response, records a span with no route. [Register it with `micro.install(app)`](changelog.md#0420-2026-10-19).
- A token with no `sub`, a `typ` other than `JWT`, `JOSE` or `at+jwt`, or a `cnf` claim is rejected. A `malformed` token no longer counts toward a ban. [Issue tokens with `sub` and a plain `typ`](changelog.md#0420-2026-10-19).
- `grelmicro check` and `micro.describe(app)` list the routes the integration declares, so some rows change name, split per method or disappear. [Nothing to change](changelog.md#0420-2026-10-19).
- `GREL_CIRCUITBREAKER_{NAME}_IGNORE_EXCEPTIONS` and `GREL_SHIELD_{NAME}_TIMEOUT_ERRORS` are no longer read. [Rename the variable to `_WHEN`](#0-42-when).
- `GREL_IDEMPOTENT_REQUESTS_WAIT_TIMEOUT` and `GREL_IDEMPOTENT_REQUESTS_{NAME}_WAIT_TIMEOUT` are no longer read. [Rename the variable to `_MAX_WAIT`](#0-42-max-wait).
- A `HEAD` is answered from the stored `GET` only on a route that answers both, and a `HEAD` route of its own runs its handler. [Declare the route](#0-42-answered-at-route).
- With `Trace` or `Metrics` registered, FastAPI no longer exports anything itself, and its log records are not exported. [Export through `Trace` and `Metrics`](changelog.md#0420-2026-10-19).
- On FastAPI, a request span follows the semantic conventions 1.44 instead of 1.11. It has `fastapi.dependencies`, `fastapi.endpoint` and `fastapi.serialization` children instead of `send` and `receive`, and a WebSocket span is named like `WS /ws/{room}`. [Update trace queries](changelog.md#0420-2026-10-19).
- On FastAPI, the access record names the route FastAPI's own request span names, for a slash redirect, a frontend route, a mounted app, a `Host` route and a prefix a proxy stripped. [Update log filters](changelog.md#0420-2026-10-19).
- On a plain Starlette app, `http.route` carries the route template in the access log and security events. [Update log filters](changelog.md#0420-2026-10-19).
- `grelmicro check`, `micro.describe(app)` and `CachedResponses` match an `include=` URL one path segment per parameter, whatever its converter, so `/items/abc` names `/items/{item_id:int}`. [Check the `include=` patterns](changelog.md#0420-2026-10-19).
- `ClientBannedError` is an `AdmissionError` carrying `retry_after`, and `ErrorResponses` answers it `429` with a `Retry-After` header. [Catch it as an `AdmissionError`](changelog.md#0420-2026-10-19).
- From 0.42.1, on Starlette, a route under a `Mount` names every mount, such as `/mm/r/a` instead of `/mm/a`. [Update route filters](changelog.md#0421-2026-10-19).

### Changes that raise {#0-42-raising}

- A float duration is refused, and `every(seconds=...)` and `misfire_grace_seconds=` are renamed. [Pass whole seconds or a `timedelta`](#0-42-durations).
- `every` and `cron` refuse `lock=` and `leader=`, and a `TaskLock` gate that holds for less than the interval is refused. [Pass `gate=`](#0-42-task-gate).
- `ignore_exceptions=`, `timeout_errors=` and the bare `@shield` are gone, and a Shield requires `when=`. [Pass `when=`](#0-42-when).
- A Shield refuses to build while `GREL_SHIELD_{NAME}_PROFILE` or `GREL_SHIELD_PROFILE` is set. [Choose the preset in code](#0-42-shield-profile-env).
- `metrics_router` no longer imports from `grelmicro.metrics`. [Import from `grelmicro.integrations.fastapi`](#0-42-metrics-router-moved).
- The `document_*` functions are removed. [Register the component](#0-42-document-functions).
- `CachedResponsesMiddleware(policies=...)` is removed. [Drop it](#0-42-hand-added).
- `CacheError` is removed. [Delete the `except CacheError:` block](#0-42-cache-error).
- `lock=` refuses `"local"`, `True` and `False`, and `"host"` or `"cluster"` with no lock backend raises `OutOfContextError`. [Name the fold scope](#0-42-fold-scope).
- `wait_timeout=` is renamed `max_wait=`, and `BulkheadConfig(max_wait=None)` is refused. [Pass `max_wait=`](#0-42-max-wait).
- `Idempotency` and `IdempotentRequests` with no lock backend raise `BackendScopeError` in `staging` and `production`. [Register a lock backend](#0-42-idempotency-lock).
- `TaskLock.refresh()` is renamed `extend()`, and a failed extension raises `LockExtendError`. [Follow the `extend()` rename](#0-42-lock-extend).
- A string passed to `key=` raises `TypeError`, and `key_maker=` is gone. [Pass `key_template=` or `key=`](#0-42-key-function).
- A `TaskLock` with no name raises when entered, and an invalid lock or task name is refused. [Update the lock name](#0-42-lock-names).
- Provider methods end in `_backend`, and a custom `Provider` with the old names fails at class definition. [Call `lock_backend()`](#0-42-backend-names).
- grelmicro needs Python 3.14, FastAPI 0.142.1, Starlette 1.7 and higher dependency floors. [Upgrade Python and the dependencies](changelog.md#0420-2026-10-19).
- The JWT API changes: `JWTKey.pem()`, `.secret()` and `.jwk()` build a key, `JWTVerifier.keys()` and `.jwks()` build a verifier, `audience` is required, and `JWKSVerifier`, `bans=` and `client=` are gone. [Use the new factories](changelog.md#0420-2026-10-19).
- A pattern holding a backend of its own goes through the backend check, and `IdempotentRequests` refuses a memory `Cache` in `production`. [Register a shared backend or pass `requires="process"`](changelog.md#0420-2026-10-19).
- `CachedResponse()` on a route that runs a check of its own fails install. [Drop `CachedResponse()` from that route](changelog.md#0420-2026-10-19).
- From 0.42.1, `Grelmicro(uses=[p1, p2])` raises `AmbiguousProviderError` when the app opens, not at construction, and `micro.use(p1)` then `micro.use(p2)` raises it too. A component of a kind a provider default holds raises `ComponentAlreadyRegisteredError` once the defaults are registered. [Name the component for each kind](changelog.md#0421-2026-10-19).

### A stored duration is whole seconds or a `timedelta` {#0-42-durations}

A lease, a TTL, a task schedule and a circuit breaker reset timeout
take whole seconds as an `int`, or a `timedelta`. A float is refused,
`60.0` included. Each config reads the value back as a `timedelta`.

```python
from datetime import timedelta

from grelmicro.coordination import Lock

# Before
tasks.every(seconds=0.5)
tasks.cron("0 * * * *", misfire_grace_seconds=600)
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
  per-path `include` TTLs, and `CachedResponse(ttl)`.
- **Rate limiter**: the sliding window `window`.
- **Circuit breaker**: `reset_timeout` on `CircuitBreaker.consecutive_count`
  and `ConsecutiveCountConfig`.
- **Security**: `ClientBans` `window` and `duration`, and `JWTVerifier` `ttl` and `cache_ttl` (`0` still turns the cache off).
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
on the previous version is not seen. Upgrade every worker at once, as
[State and rolling deploys](#0-42-rolling-deploy) explains.

### State and rolling deploys {#0-42-rolling-deploy}

Four kinds of stored state move to new keys in 0.42. During a rolling or
blue/green deploy, old and new workers share the backend, but each version
reads and writes only its own keys.

| State | 0.41 | 0.42 | While both versions run |
|---|---|---|---|
| Sliding window rate limit, on Redis, Postgres and SQLite | keys under `gcra:`, Postgres `<table>_gcra_*` functions | keys under `gcra_us:`, Postgres `<table>_gcra_us_*` functions | Each version counts on its own, so a caller gets up to twice the limit. A caller locked out on 0.41 starts with a full budget on 0.42. |
| Redis leader election | record under `<name>` | record under `le_us:<name>` | Each version elects its own leader. |
| An interval task whose function name is not a valid lock name | lock named `__main__:job` | lock named `task-__main__:job`, see [lock names](#0-42-lock-names) | The task runs once per version. |
| A cached response on a route that declares its protection | key without the route's protection | key under the route's protection | Each such response is cached again, and each version serves its own copies. |

A token bucket keeps its keys.

For leader election and an interval task with a derived lock name, stop every
0.41 worker before the first 0.42 worker starts.

For a sliding window, the doubled budget lasts as long as the overlap. grelmicro
ships no migration for the old counts, because they count a different unit. If
a sliding window guards a security budget, such as failed sign-ins, expect
every lockout to clear at the upgrade and keep the overlap short. The old Redis
keys expire on their own within one window. The old Postgres and SQLite rows
stay, as do the old Postgres functions. Once no 0.41 worker runs, you can
delete them. With the default table and an empty `prefix`:

```sql
DELETE FROM grelmicro_rate_limiter WHERE substr(key, 1, 5) = 'gcra:';
```

With a `prefix`, put it before `gcra:` and count the whole string, such as
`substr(key, 1, 9) = 'app:gcra:'`. Avoid `LIKE`, where a `_` in the prefix
matches any character.

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

In 0.42.0, a cron task with no `gate=` runs each fire on every worker and
nothing reports it. Pass `gate="claim"` to keep one worker per fire. From
0.42.1, such a task warns [`ungated-cron`](diagnostics.md#ungated-cron) when it
starts beside a schedule backend. Pass `gate=None` to keep every worker on
purpose.

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
Name each by its full path, such as `builtins.ValueError` or
`httpx.HTTPError`.

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

`document_idempotency`, `document_conditional_requests` and `document_rate_limited_requests` are gone from `grelmicro.integrations.fastapi`. A registered component needs nothing: `micro.install(app)` describes it in the OpenAPI schema on FastAPI and Litestar.

A middleware you added by hand and documented with one of them is registered as its component instead, with the same options:

```python
# Before
app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))
document_idempotency(app)

# After
micro = Grelmicro(uses=[cache, IdempotentRequests()])
micro.install(app)
```

### A third-party integration declares its routes {#0-42-route-hooks}

This is for authors of an integration published through the
`grelmicro.integrations` entry point. The response cache, idempotency and
conditional requests decide per route, once the framework routed the request,
from the declarations the integration hands them. `grelmicro check` and
`micro.describe(app)` list the routes those declarations name. An integration
without the hooks declares no route, and `grelmicro check` warns
`routes-not-declared`.

Ship both hooks in the integration module, built from one function so they
cannot disagree:

```python
from grelmicro.http import RouteDeclaration


def route_declarations(app):
    return [_declare(route) for route in app.routes]


def install_route_gate(app, gate):
    for route in app.routes:
        route.app = gate(route.app, _declare(route))


def _declare(route):
    return RouteDeclaration(
        route.path,
        methods=frozenset(route.methods),
        anonymous=route.anonymous,
        scopes=frozenset(route.scopes),
    )
```

`install_route_gate` replaces what the router dispatches to with the app the
gate returns. [Declare the routes](architecture/plugins.md#declare-the-routes)
lists every field and rule.

### The cache, idempotency and conditional requests answer at the route {#0-42-answered-at-route}

`CachedResponses`, `IdempotentRequests` and `ConditionalRequests` run at the route on every app.

- A URL no route answers is neither cached nor replayed, such as the `200` of a not-found handler of your own. Declare a route for what has to be cached or replayed.
- A `HEAD` is read from the stored `GET` only on a route that answers both.
- `require_precondition=` and `require_key=True` answer `428` and `400` only on a route that takes the request. An unknown URL gets `404`, and a method the route does not take gets `405`.
- An `include=` pattern never caches a route under middleware on a mount, a router or a mounted app. Declare `CachedResponse()` on such a route when its response is the same for every caller.

The rate limit still counts every request in front of the router.

### A middleware added by hand reads no route {#0-42-hand-added}

`CachedResponsesMiddleware`, `IdempotencyMiddleware` and `ConditionalRequestsMiddleware` added by hand no longer read the app's routes. Each one decides from its own `include=` and `exclude=` and the request headers. A request carrying `Authorization` or `Cookie` still skips the cache and the default idempotency key, and a response setting a cookie is still not stored. A route's own checks, such as an authentication dependency or middleware on a mount that reads a tenant header, are not seen there, and neither is its `CachedResponse()`. Register the component, which reads them:

```python
# Before
app.add_middleware(
    CachedResponsesMiddleware, cache=TTLCache(), include={"/products/*": 60}
)

# After
micro = Grelmicro(uses=[..., CachedResponses(include={"/products/*": 60})])
micro.install(app)
```

Or keep it added by hand and leave such routes out of `include=`. `CachedResponsesMiddleware(policies=...)` is removed, so drop it.

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
| `async with idem(key, wait_timeout=5)` | `async with idem(key, max_wait=5)` |
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
block the new ones, so upgrade every worker running such a task at once, as
[State and rolling deploys](#0-42-rolling-deploy) explains.

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

Full list: [0.42.0](changelog.md#0420-2026-10-19) and [0.42.1](changelog.md#0421-2026-10-19) in the changelog

## 0.41

### Changes that do not raise {#0-41-silent}

- Metric labels gain the `grelmicro.` prefix, and some metrics change name. A dashboard or an alert on the old names goes blank. [Update the queries](#0-41-metric-names).
- `/livez`, `/readyz`, `/healthz` and `/metrics` leave the OpenAPI schema. [Pass `include_in_schema=True`](#0-41-health-openapi).
- JSON logs use orjson when it is installed, which renders some values differently. [Pin the serializer](#0-41-json-serializer).
- An idempotent request carrying `Authorization` or `Cookie`, on an authenticated Starlette scope, or to a FastAPI route with dependencies runs the app every time. Entries stored before 0.41 are not read. [Pass a key function](#0-41-idempotency-bypass).
- Uvicorn's own lines move from stderr to the stream the app writes to. [Turn the pass off](#0-41-uvicorn-stream).
- A decorator on an object with an async `__call__` now applies to the call. [Nothing to change](#0-41-async-callable-objects).
- A `cached(ttl=...)` decorator applied to several functions builds one cache per function. [Pass a `TTLCache` to share one](changelog.md#0410-2026-09-12).
- `CachedResponses(vary_by_query=None)` keys on the whole query string, whatever the environment says. [Leave it unset to read the environment](changelog.md#0410-2026-09-12).
- `IdempotencyMiddleware` answers `400`, `409`, `413` and `422` as `application/problem+json`. A client reading `detail` still finds it. [Nothing to change](changelog.md#0410-2026-09-12).
- An `Idempotency-Key` holding a byte outside printable ASCII is answered `400`. [Send printable ASCII keys](changelog.md#0410-2026-09-12).
- A problem detail never carries the exception message, and every error response carries `Cache-Control: no-store`. [Nothing to change](changelog.md#0410-2026-09-12).
- Access logs keep query parameter names but mask every value. [Nothing to change](changelog.md#0410-2026-09-12).
- A bounded lock acquire that runs out raises `LockTimeoutError` and answers `503`, not `500`. `except TimeoutError` still catches it. [Nothing to change](changelog.md#0410-2026-09-12).
- `Timeout` raises `DeadlineExceededError`, which `except TimeoutError` still catches. [Nothing to change](changelog.md#0410-2026-09-12).
- `TaskRouter(tasks=[...])` and `Tasks(tasks=[...])` copy the list they are given. [Add tasks to the router, not the list](changelog.md#0410-2026-09-12).
- A `Bulkhead`'s `uses=` answers before `micro.fake()` and `micro.override(...)`. [Build the bulkhead with the test backends](changelog.md#0410-2026-09-12).

### Changes that raise {#0-41-raising}

- `GrelmicroMiddleware` imports from `grelmicro`, and `IdempotencyMiddleware` and `StoredResponse` from `grelmicro.http`. [Change the imports](changelog.md#0410-2026-09-12).
- The `fastapi` extra needs FastAPI 0.137 or later. [Upgrade FastAPI](changelog.md#0410-2026-09-12).
- A grelmicro decorator refuses a function a registrar such as `@tasks.every` already holds. [Put the registering decorator on top](changelog.md#0410-2026-09-12).
- `IdempotentRequests` takes `ttl=`, `namespace=` and `cache=` instead of an `Idempotency`, and `key_header=` instead of `header=`. [Pass `ttl=` and `key_header=`](changelog.md#0410-2026-09-12).
- `CachedResponses`, `ConditionalRequests`, `IdempotentRequests`, `RateLimitedRequests` and `AccessLog` raise `SettingsValidationError` for a bad value, not `TypeError` or `ValueError`. [Catch `SettingsValidationError`](changelog.md#0410-2026-09-12).
- `CachedResponses(include=...)` refuses a pattern for a path that answers no `GET`. [Point the pattern at a `GET` route](changelog.md#0410-2026-09-12).
- `max_body_size=0`, a bare string for `methods=` or `require_precondition=`, and `bytes` for a header name are refused. [Pass a positive size, a tuple and a `str`](changelog.md#0410-2026-09-12).
- `AccessLogMiddleware` no longer carries `include`, `exclude`, `quiet`, `query` and `user_agent` as attributes. [Read them from the `AccessLog` component](changelog.md#0410-2026-09-12).
- A `Bulkhead` refuses two Components claiming one `(kind, name)` in `uses=`. [List each kind once](changelog.md#0410-2026-09-12).
- Every `Match` argument error is a `ValueError` at construction. [Catch `ValueError`](changelog.md#0410-2026-09-12).
- A shield's `timeout_errors=` refuses a `BaseException`-only type, a set and a generator. [Pass a tuple of `Exception` classes](changelog.md#0410-2026-09-12).
- A custom `CircuitBreakerStrategy` has to implement `abandon()`. [Implement `abandon()`](changelog.md#0410-2026-09-12).
- The Postgres circuit breaker recreates three stored functions on start. [Keep `auto_migrate` on or apply the change yourself](changelog.md#0410-2026-09-12).
- `Lock.do_thread_*` take the thread's identity. [Use `lock.from_thread`](changelog.md#0410-2026-09-12).

### Metric names and labels {#0-41-metric-names}

Every metric attribute grelmicro sets carries the `grelmicro.` prefix.
`outcome`, `result` and `decision` become one attribute, `grelmicro.outcome`.
`task.name`, `check.name`, `retry.name`, `circuit_breaker.name`,
`bulkhead.name`, `timeout.name`, `rate_limiter.name`, `idempotency.name` and
`topic` gain the prefix too. `error.type` is unchanged.

A query on an old label matches nothing and raises no error. Prometheus writes
the dots as underscores, so the `task_name` label becomes `grelmicro_task_name`.

Some metrics change name or meaning:

| 0.40 | 0.41 |
|---|---|
| `grelmicro.bulkhead.rejections` | `grelmicro.bulkhead.calls`, `grelmicro.outcome` is `admitted` or `rejected` |
| `grelmicro.rate_limiter.decisions`, `decision` is `allowed` or `limited` | `grelmicro.rate_limiter.calls`, `grelmicro.outcome` is `admitted` or `rejected` |
| `grelmicro.circuit_breaker.transitions` with `from` and `to` | the same metric with `grelmicro.circuit_breaker.previous_state` and `grelmicro.circuit_breaker.state` |
| `grelmicro.retry.attempts`, one point per run | one point per attempt |

```promql
# Before
rate(grelmicro_rate_limiter_decisions_total{decision="limited"}[5m])

# After
rate(grelmicro_rate_limiter_calls_total{grelmicro_outcome="rejected"}[5m])
```

`grelmicro.task.duration` and `grelmicro.retry.duration` carry
`grelmicro.outcome`. `grelmicro.cache.operations`, `.stale_serves`,
`.early_refreshes` and `grelmicro.shield.cache_writes` carry the instance name,
so two caches report two series. [Metrics](metrics.md) lists every name.

### Health and metrics routes leave the OpenAPI schema {#0-41-health-openapi}

`health_router()` keeps `/livez`, `/readyz` and `/healthz` out of the OpenAPI
schema, and `metrics_router()` keeps `/metrics` out. The routes answer as
before. Only the schema changes, so a contract diff or a generated client sees
them go. Pass `include_in_schema=True` to publish them:

```python
# Before: published by default
app.include_router(health_router())
app.include_router(metrics_router())

# After
app.include_router(health_router(include_in_schema=True))
app.include_router(metrics_router(include_in_schema=True))
```

A published route is described as what it sends. `/livez` and `/readyz` carry
no content, and `/metrics` carries Prometheus text. Both used to read as JSON.

### JSON logs use orjson when it is installed {#0-41-json-serializer}

`GREL_LOG_JSON_SERIALIZER` defaults to `auto`, where it was `stdlib`. `auto`
uses orjson when it is installed and the standard library when it is not.
`grelmicro[standard]` installs orjson. This reverses 0.35.1, which kept
`stdlib` and did not pick orjson just because it was installed.

The two write some values differently. orjson writes a `UUID`, an `Enum` and a
dataclass as their value, and non-ASCII text as UTF-8. The standard library
writes their `repr` and escapes non-ASCII text. With orjson, `NaN` and
`Infinity` become `null`. A parser or an alert matching
the exact text of such a field can stop matching. Pin the serializer to keep
the 0.40 output:

```bash
GREL_LOG_JSON_SERIALIZER=stdlib
```

The variable applies with `GREL_ENV_LOAD` on. In code, pass
`configure(json_serializer="stdlib")`. [Which serializer you
get](logging/index.md#which-serializer-you-get) compares the two.

### An authenticated request is never replayed {#0-41-idempotency-bypass}

Without a key function of your own, the default idempotency key no longer
replays a response across callers. These requests skip idempotency and run the
app every time:

- a request carrying `Authorization` or `Cookie`
- a request on an authenticated Starlette scope
- a FastAPI route with dependencies, such as authentication that reads a custom header

A required key is still checked first, so `require_key=True` still answers
`400` without one. To replay authenticated requests, fold the caller's identity
into a key function, as [Keys are scoped per
route](http/idempotency.md#keys-are-scoped-per-route) shows. In 0.41 it is
`key_maker=`, and from 0.42 it is `key=`.

The built-in key moves to a new `v3:` format, so 0.41 does not read an entry an
older version stored. A request a client retries across the upgrade runs again.
A key function of your own keeps its keys.

From 0.42, the default key follows the checks each route declares, and a
FastAPI route with only grelmicro's own dependencies replays again.

### Uvicorn writes to the app's stream {#0-41-uvicorn-stream}

`configure()` moves uvicorn's own lines from `sys.stderr` to the stream the
rest of the process writes to. A shell redirect such as `2>` that kept
uvicorn's startup and error lines apart no longer catches them. A handler
already pointed elsewhere keeps its stream, and a `FileHandler` keeps its file.
Pass `configure(uvicorn_enabled=False)` to leave uvicorn's logging alone.

### Decorators read an async callable object as async {#0-41-async-callable-objects}

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
above lists four rows:

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

Full list: [0.41.0 in the changelog](changelog.md#0410-2026-09-12)

## 0.40

### Changes that do not raise {#0-40-silent}

- An error no longer quotes the value it rejected, an unknown timezone name included. It names a close match instead. [Nothing to change](changelog.md#0400-2026-08-18).

### Changes that raise {#0-40-raising}

- The per-module `*SettingsValidationError` classes are gone, and `Fallback`, `Shield`, `TTLCache`, `cached()`, `TrustedProxies` and `ExternalConfig` raise `SettingsValidationError`. [Catch the base error](#0-40-one-settings-error).
- `Grelmicro(environment=...)` refuses a value outside the four tiers. [Name a real tier](#0-40-environment-validated).
- A bad `Match` argument or `when=` raises `ValueError`, not `TypeError`. [Catch `ValueError`](#0-40-match-value-error).
- `RedisProviderConfigError` and `PostgresProviderConfigError` are removed. [Catch the base error](#0-40-provider-errors).

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

Full list: [0.40.0 in the changelog](changelog.md#0400-2026-08-18)

## 0.39

### Changes that do not raise {#0-39-silent}

- A filter, a level or a handler on the `grelmicro.clientip` logger stops matching. [Rename the logger](#0-39-clientip-moved).
- With `GREL_ENV_LOAD` on, `CircuitBreaker`, `RateLimiter` and the `Shield` factories read their `GREL_*` variables at construction. A variable they ignored now applies. [Check the `GREL_*` variables](changelog.md#0390-2026-08-16).

### Changes that raise {#0-39-raising}

- `grelmicro.clientip` no longer imports. [Import from `grelmicro.security`](#0-39-clientip-moved).
- A named `CircuitBreaker`, `RateLimiter` or `Shield` refuses at startup a variable for an algorithm the code does not run. [Remove the other algorithm's variable](changelog.md#0390-2026-08-16).
- `CircuitBreaker` takes no positional config, `RateLimiter` has no bare constructor, and nine patterns lose `config=`. [Use `from_config` or a factory](changelog.md#0390-2026-08-16).
- A bad value raises `SettingsValidationError` instead of `pydantic.ValidationError`. [Catch `SettingsValidationError`](changelog.md#0390-2026-08-16).

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

Full list: [0.39.0 in the changelog](changelog.md#0390-2026-08-16)

## 0.38

### Changes that do not raise {#0-38-silent}

- A variable under the bare `GREL_{PATTERN}_` prefix applies to every instance, not only the default one. [Name the default instance](#0-38-bare-prefix).

### Changes that raise {#0-38-raising}

None.

### A bare variable reaches every instance {#0-38-bare-prefix}

`GREL_LOCK_LEASE_DURATION=60` used to configure `Lock("default")` alone. It now
sets the lease of every `Lock` that does not set its own
`GREL_LOCK_{NAME}_LEASE_DURATION`. Every pattern reads its bare prefix the same
way. To keep a value on the default instance only, name it:

```bash
# Before
GREL_LOCK_LEASE_DURATION=60

# After
GREL_LOCK_DEFAULT_LEASE_DURATION=60
```

Full list: [0.38.0 in the changelog](changelog.md#0380-2026-08-15)

## 0.37

### Changes that do not raise {#0-37-silent}

None.

### Changes that raise {#0-37-raising}

- `RateLimiterRegistry` and `CircuitBreakerRegistry` are renamed, and `health_router(registry=...)` takes `component=`. [Rename to `Component`](#0-37-registry-renamed).
- From 0.37.1, a provider URL is checked on every path, so a URL a client library accepted can be refused at construction. [Fix the URL](#0-37-1-url-validation).

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

### A provider URL is validated on every path {#0-37-1-url-validation}

From 0.37.1, a URL passed to a provider constructor is checked against the
same type as one read from the environment. Both paths accept the same URLs
and raise the same error, so a URL the client library used to accept can now
be refused where it used to connect:

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

Full list: [0.37.0](changelog.md#0370-2026-08-11) and [0.37.1](changelog.md#0371-2026-08-13) in the changelog

## 0.36

### Changes that do not raise {#0-36-silent}

None.

### Changes that raise {#0-36-raising}

- `LogTimeZoneType` is removed. [Use `TimeZoneName`](#0-36-timezone-type).
- A timezone abbreviation such as `CEST` is refused where the value is read. [Name the zone](#0-36-timezone-abbreviation).
- Starting `Tasks` twice raises `TaskStartOperationError` instead of `TaskAddOperationError`. [Catch `TaskStartOperationError`](changelog.md#0360-2026-08-09).
- A negative `Tasks(shutdown_timeout=...)` raises `TaskSettingsValidationError` instead of a plain `ValueError`. It still subclasses `ValueError`. [Catch `ValueError`](changelog.md#0360-2026-08-09).

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

Full list: [0.36.0 in the changelog](changelog.md#0360-2026-08-09)

## 0.35

### Changes that do not raise {#0-35-silent}

- A passing `/healthz` check leaves out `error`, and a check with no details leaves out `details`. [Read an absent `error` as a pass](#0-35-healthz-fields).
- Listing a Component no longer turns off every provider default. [Drop the workaround](#0-35-per-kind-defaults).
- `REDIS_PASSWORD` beside a `REDIS_URL` is used, where it was dropped. [Set `REDIS_URL` and `REDIS_PASSWORD`](#0-35-redis-password).
- From 0.35.1, uvicorn's own lines follow your log format. [Update the parser or turn it off](#0-35-1-uvicorn-format).
- From 0.35.1, a `GREL_*` variable set without `GREL_ENV_LOAD` warns `GrelmicroConfigWarning`. It raises only under `-W error`. [Fix the configuration](#0-35-1-config-warning).

### Changes that raise {#0-35-raising}

- A Redis or Valkey URL that carries credentials is refused beside a separate password. [Keep the password in one place](#0-35-redis-password).

### `/healthz` leaves out empty fields {#0-35-healthz-fields}

A passing check no longer carries `error`, and a check with no details no
longer carries `details`:

```diff
-{"status": "ok", "critical": true, "error": null}
+{"status": "ok", "critical": true}
```

A consumer that reads `error` on every check treats it as absent:

```python
error = check.get("error")  # None when the check passed
if error is not None:
    alert(name, error)
```

A failing check still carries `error`, and every entry still carries `status`
and `critical`.

### A Component claims only its own kind {#0-35-per-kind-defaults}

Listing any Component used to turn every provider default off. An app that
added a `HealthChecks` lost its cache and its locks with no warning. Now a
Component claims its own kind and the Provider fills the rest. A Component of a
kind no Provider serves, such as `HealthChecks`, `Log` or `Trace`, claims
nothing. Two or more Providers still fill no defaults.

If you listed every component only to work around that, the short form works:

```python
micro = Grelmicro(uses=[redis, tasks, health])
```

### A Redis password beside the URL is used {#0-35-redis-password}

`REDIS_PASSWORD` next to a `REDIS_URL` was read and then dropped, so the client
connected without it. It now authenticates. `redis+sentinel://` and
`redis+cluster://` are accepted from the environment and from `RedisConfig`,
the multi-host form included:

```bash
REDIS_URL=redis+sentinel://a:26379,b:26379/mymaster/0
REDIS_PASSWORD=...
```

A URL that already carries credentials, beside a separate `REDIS_PASSWORD`, now
raises. Keep the password in one place. The same applies to `VALKEY_*`.

### Uvicorn's lines follow your log format {#0-35-1-uvicorn-format}

From 0.35.1, `configure()` applies your format to uvicorn's loggers. A line
that looked like this:

```text
INFO:     127.0.0.1:54321 - "POST /orders HTTP/1.1" 200 OK
```

now carries a timestamp, a level field and structured request fields, like
the rest of the app. Update a pipeline that parses uvicorn's plain format, or
keep the old lines:

```python
configure(uvicorn_enabled=False)
```

### An ignored `GREL_*` variable warns {#0-35-1-config-warning}

From 0.35.1, a `GREL_*` variable set without `GREL_ENV_LOAD` warns
`GrelmicroConfigWarning`. A suite run with `-W error`, or pytest with
`filterwarnings = error`, fails on it. Fix the configuration:

```bash
GREL_ENV_LOAD=1
```

Or pass the value in code, which never needs the flag. To keep the warning
visible without failing the build, filter the category:

```toml
filterwarnings = ["error", "ignore::grelmicro.GrelmicroConfigWarning"]
```

Full list: [0.35.0](changelog.md#0350-2026-08-05) and [0.35.1](changelog.md#0351-2026-08-06) in the changelog

## 0.34

### Changes that do not raise {#0-34-silent}

- `grelmicro.task.runs` counts every fire, so its total grows with the number of workers. [Filter on the outcome](#0-34-task-run-outcomes).

### Changes that raise {#0-34-raising}

- `@cached` on a method without a key raises `TypeError` at decoration. [Name the key](#0-34-cached-method-key).

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

### Task run totals count every fire {#0-34-task-run-outcomes}

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

Full list: [0.34.0 in the changelog](changelog.md#0340-2026-08-02)

## 0.33

### Changes that do not raise {#0-33-silent}

None.

### Changes that raise {#0-33-raising}

None.

Full list: [0.33.0 in the changelog](changelog.md#0330-2026-07-31)

## 0.32

### Changes that do not raise {#0-32-silent}

- `url` and `endpoint` fields read back as `SecretUrl`, and each `headers` value as `SecretStr`. [Call `.get_secret_value()`](#0-32-secret-urls).
- From 0.32.2, an open circuit on Postgres or SQLite closes once after the upgrade. [Nothing to change](#0-32-2-circuit-breaker-state).

### Changes that raise {#0-32-raising}

- `SQLiteLockAdapter` and `SQLiteScheduleAdapter` take `provider=`, not a path. [Pass `provider=`](#0-32-sqlite-provider).

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

### An open circuit closes once {#0-32-2-circuit-breaker-state}

From 0.32.2, circuit breakers reclaim their stored state instead of keeping it
forever. Rows written before 0.32.2 carry no activity timestamp, so an
already-open circuit on Postgres or SQLite reads as expired the first time
the new code touches it and starts again from `CLOSED`.

This happens once, on the first call after the upgrade. Circuits held open
by `isolate()` are unaffected.

Full list: [0.32.0](changelog.md#0320-2026-07-28) and [0.32.2](changelog.md#0322-2026-07-28) in the changelog

## 0.31

### Changes that do not raise {#0-31-silent}

- Credential fields read back as `SecretStr`. [Call `.get_secret_value()`](#0-31-secret-credentials).
- `Metrics()` with no endpoint exports nothing, where it sent to `localhost:4318`. [Set an endpoint](#0-31-metrics-auto-exporter).

### Changes that raise {#0-31-raising}

None.

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

Full list: [0.31.0 in the changelog](changelog.md#0310-2026-07-27)

## 0.30

### Changes that do not raise {#0-30-silent}

None.

### Changes that raise {#0-30-raising}

- `Operation.response` is now a `result()` method. [Use `result()`](#0-30-operation-result).

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

Full list: [0.30.0 in the changelog](changelog.md#0300-2026-07-18)

## About deprecation

Before 1.0 a rename is a clean cut: the old name is removed in the same
release the new one appears, with no deprecation cycle and no alias. That
keeps a fast-moving `0.x` from accumulating shims, and it is why this page
exists instead.

After 1.0, `1.x` follows standard semver and breaking changes go through a
deprecation cycle.
