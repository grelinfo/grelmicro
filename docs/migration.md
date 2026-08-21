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
from grelmicro.log import LogSettingsValidationError

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

```python
async with idem(key) as op:
    if op.replayed:
        return op.result()  # was: op.response
    ...
```

It is valid **only** on a replay. Calling it on a first execution raises
`IdempotencyStateError`, so keep it behind `if op.replayed:`.

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
rate(grelmicro_task_runs_total{outcome="success"}[5m])
```

Nothing else changes. `outcome="success"` and `outcome="error"` keep the
meanings they had, so an alert already filtering on either is correct as
it stands.

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
