# Migration

One note per minor release, listing only what you have to change. Each entry
names the symptom first, so you can match what you are seeing rather than
read the whole page.

This page covers **0.30 onward**. For an older version, read the `Breaking`
sections in the [changelog](changelog.md), newest first.

In `0.x` the minor is the breaking position: a `0.x.0` release may change the
public API, and a `0.x.y` release is a safe patch. So an upgrade within one
minor rarely needs this page. Two patches are exceptions, and neither changes
an API: [0.32.2](#0-32-2-circuit-breaker-state) changes what an already-open
circuit does once, and [0.33.1](#0-33-1-task-run-outcomes) changes what a task
metric counts.

## Find your symptom

| Symptom | Release | Fix |
|---|---|---|
| `AttributeError: 'Operation' object has no attribute 'response'` | 0.30 | [Use `result()`](#0-30-operation-result) |
| A password or URL reads back as `SecretStr(...)` or `***` | 0.31, 0.32 | [Call `.get_secret_value()`](#0-31-secret-credentials) |
| Metrics stopped arriving after upgrading, with no error | 0.31 | [Set an endpoint](#0-31-metrics-auto-exporter) |
| `TypeError` on a SQLite adapter, either `unexpected keyword argument 'path'` or `takes 1 positional argument` | 0.32 | [Pass `provider=`](#0-32-sqlite-provider) |
| `SettingsValidationError` where you caught `CoordinationSettingsValidationError` | 0.32 | [Catch the base error](#0-32-sqlite-provider) |
| An open circuit closed once, just after upgrading | 0.32.2 | [Expected, happens once](#0-32-2-circuit-breaker-state) |
| Task run totals jumped after upgrading, with no new failures | 0.33.1 | [Filter on `outcome`](#0-33-1-task-run-outcomes) |

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

A missing path now raises `SettingsValidationError` instead of
`CoordinationSettingsValidationError`. If you catch the latter, catch the
base error.

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

## 0.33.1, not breaking but worth knowing {#0-33-1-task-run-outcomes}

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
