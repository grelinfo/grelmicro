# Configuration

You build a pattern with keyword arguments. You tune it in deployment with
environment variables. No code change between the two.

## How a value is resolved

A field takes the first of these that supplies it:

1. **A keyword argument.** Always wins, always available, needs nothing enabled.
2. **A `GREL_*` environment variable**, when `GREL_ENV_LOAD` is truthy. Off by
   default.
3. **A file**, through
   [`ExternalConfig`](configuration/reconfigure-from-configmap.md), which also
   reconfigures a running component when the file changes.
4. The field's default.

The flag gates the `GREL_*` namespace, which is grelmicro's own. It does not
gate a [Provider](providers/index.md), and it never did. `RedisProvider()` reads
`REDIS_URL`, `PostgresProvider()` reads `POSTGRES_URL`, with no flag, because
those names belong to your environment rather than to grelmicro. A provider
variable that is missing fails at construction and names the variable it
wanted, so there is no silent default to protect you from. Pass
`env_load=False` to a provider to turn its env reads off.

Two `GREL_*` names sit outside the flag as well, because neither fills a
component field. `GREL_ENV_LOAD` is the flag. `GREL_ENVIRONMENT` says where
the app runs, which decides how a backend that cannot keep a promise across
replicas is reported: see
[the backend check](deployment.md#the-backend-check).

Step 2 is the one that surprises people. `GREL_ENV_LOAD` is a single
process-wide switch, so a variable set without it is ignored and the default
applies. That situation reports at startup instead of passing silently, naming
the variable:

```
UserWarning: GREL_LOG_FORMAT is set but was not applied: environment-driven
configuration is opt-in. Set GREL_ENV_LOAD=1 to enable it, or pass the value
directly.
```

The same report is logged on the `grelmicro` logger once logging is configured,
so on the default backend it also lands in the JSON log stream, where a warning
on stderr would be lost. [Deployment](deployment.md) covers where to set the
switch in a container.

The switch exists because step 2 fills *every* field you did not pass, not only
the ones with no default. An app that passes some settings from its own config
object and leaves the rest would otherwise split one component's configuration
across two sources without saying so. Opting in makes that a decision rather
than an accident. [Config resolution](advanced/config.md) has the full contract.

### Local development without exporting variables

Two options, and they cover different things.

**Put the variables in a `.env` and load them into the process.** Anything that
populates `os.environ` works, and relative paths are fine:

```bash
# .env
GREL_ENV_LOAD=1
GREL_LOG_FORMAT=PRETTY
GREL_LOCK_CART_LEASE_DURATION=60
```

```bash
uv run --env-file .env python -m myapp
```

`GREL_ENV_LOAD=1` has to be in the file too, or nothing else in it is read.

**Or pass the values in code**, which needs no switch at all and is the only
option for logging:

```python
from grelmicro.log import configure

configure(format="PRETTY")
```

`ExternalConfig` reads a `.env` as well, but it feeds *reconfigurable*
components (locks, retries, timeouts, health checks) and not `Log`, which
installs logging once at startup. For log format in local development, use
`configure(...)` or the loaded-environment route above.

## Build with keyword arguments

Pass the name first, then the settings:

```python
from grelmicro.coordination import Lock

lock = Lock("cart", lease_duration=60, retry_interval=0.1)
```

Patterns with variants use factory methods:

```python
from grelmicro.resilience import RateLimiter

api = RateLimiter.sliding_window("api", limit=100, window=60)
```

That is the whole story for code. Every value lives next to the pattern, easy to
read and easy to test.

## Tune with environment variables

The deployment overrides any field without touching code. Set the environment
variable for the field and grelmicro reads it at startup.

--8<-- "env_gate.md"

The variable name is built from the pattern and the instance name:

```
GREL_{PATTERN}_{NAME}_{FIELD}
```

A `Lock("cart")` reads its lease duration from `GREL_LOCK_CART_LEASE_DURATION`:

```bash
export GREL_LOCK_CART_LEASE_DURATION=120
export GREL_LOCK_CART_RETRY_INTERVAL=0.2
```

Names with hyphens, dots, slashes, or colons normalise into uppercase segments
(`payments-eu` becomes `PAYMENTS_EU`, `cart.v2` becomes `CART_V2`).

The instance name becomes the namespace, and the bare `GREL_{PATTERN}_` prefix
is the default for the whole pattern. Every instance falls back to it, so one
variable retunes them all:

```bash
export GREL_LOCK_LEASE_DURATION=60   # every Lock in the service
```

Name an instance to carve out an exception:

```bash
export GREL_LOCK_LEASE_DURATION=60          # every Lock
export GREL_LOCK_CHECKOUT_LEASE_DURATION=300   # except this one
```

That is the whole rule. A twelve-lock service changes one variable, not twelve.

Because the bare prefix is shared, name your instances so their segment cannot
start a field name of the same pattern. A `Lock("lease")` reads
`GREL_LOCK_LEASE_DURATION` for a field named `duration`, which is the key every
lock already reads for `lease_duration`. This is rare, and renaming the
instance is the fix.

A value passed in code wins over both variables. So a hard-coded
`Lock("cart", lease_duration=60)` ignores `GREL_LOCK_CART_LEASE_DURATION` and
`GREL_LOCK_LEASE_DURATION`. Leave a field out of the constructor to let the
deployment set it.

### Prefix reference

| Pattern | Prefix | Falls back to |
|---|---|---|
| `Lock("default")` | `GREL_LOCK_` | it is the pattern default |
| `Lock("cart")` | `GREL_LOCK_CART_` | `GREL_LOCK_` |
| `TaskLock("etl")` | `GREL_TASKLOCK_ETL_` | `GREL_TASKLOCK_` |
| `LeaderElection("svc")` | `GREL_LEADERELECTION_SVC_` | `GREL_LEADERELECTION_` |
| `RateLimitFilter()` | `GREL_RATELIMITFILTER_` | it is the pattern default |
| `RateLimitFilter(env_name="audit")` | `GREL_RATELIMITFILTER_AUDIT_` | `GREL_RATELIMITFILTER_` |
| `DuplicateFilter()` | `GREL_DUPLICATEFILTER_` | it is the pattern default |
| `DuplicateFilter(env_name="audit")` | `GREL_DUPLICATEFILTER_AUDIT_` | `GREL_DUPLICATEFILTER_` |
| `HealthChecks()` | `GREL_HEALTH_` | it is the pattern default |
| `log.configure()` | `GREL_LOG_` | it is the pattern default |
| `Tasks()` | `GREL_TASK_` | it is the pattern default |

Each pattern page lists its own fields and the exact variable names.

### One timezone for the whole service

Most services run on a single wall clock. `GREL_TIMEZONE` says which one:

```bash
export GREL_ENV_LOAD=1
export GREL_TIMEZONE=Europe/Zurich
```

Cron tasks now fire on Zurich wall-clock time, and log timestamps render in
Zurich too. It carries no pattern segment, because it belongs to the process
rather than to one component.

A component variable is more specific, so it wins. Keep logs on UTC under a
Zurich service with:

```bash
export GREL_TIMEZONE=Europe/Zurich
export GREL_LOG_TIMEZONE=UTC
```

The full order for a cron fire time, first one that supplies it:

1. `@tasks.cron(..., timezone="...")` on the task.
2. `TaskRouter(timezone="...")` on the nearest router that declares one.
3. `Tasks(timezone="...")`.
4. `GREL_TASK_TIMEZONE`.
5. `GREL_TIMEZONE`.
6. `"UTC"`.

Names are IANA names such as `Europe/Zurich`, in any casing. grelmicro
ignores the POSIX `TZ` variable on purpose. `TZ` falls back to UTC without
complaint when it cannot parse a value, which would turn a typo into a
schedule running at the wrong hour. Note that `TZ` still decides what a
naive `datetime.now()` returns inside your own task bodies.

`GREL_TIMEZONE` is read once at startup. See
[Live reconfiguration](configuration/reconfigure-from-configmap.md) for what
that means for a mounted ConfigMap.

### Defaults reference

The most important defaults for operators. All times are in seconds. Override
any of them with the pattern prefix above plus the field name, uppercased.

| Pattern | Field | Default |
|---|---|---|
| `Lock` | `lease_duration` | `60` |
| `Lock` | `retry_interval` | `0.1` |
| `TaskLock` | `lease_duration` | `60` |
| `TaskLock` | `min_hold_duration` | `1` |
| `LeaderElection` | `lease_duration` | `15` |
| `LeaderElection` | `renew_deadline` | `10` |
| `LeaderElection` | `retry_interval` | `2` |
| `LeaderElection` | `backend_timeout` | `5` |
| `TTLCache` | `ttl` | `60` |
| `RateLimiter` | `fail_open` | `False` |
| `CircuitBreaker` | `error_threshold` | `5` |
| `CircuitBreaker` | `success_threshold` | `2` |
| `CircuitBreaker` | `reset_timeout` | `30` |
| `Retry` | `attempts` | `3` |
| `Retry` | `backoff.base_delay` | `0.1` |
| `Retry` | `backoff.max_delay` | `30` |
| `Bulkhead` | `max_concurrent` | `None` (unbounded) |
| `HealthChecks` | `timeout` | `5` |
| `HealthChecks` | `cache_ttl` | `1` |
| `Idempotency` | `ttl` | `86400` (1 day) |
| `Tasks` | `timezone` | `UTC` |
| `Tasks` | `shutdown_timeout` | `30` |

`TTLCache` and `Idempotency` set their `ttl` in code, not from the environment.
`Timeout.seconds`, `Fallback.when`, and `Fallback.default` are required and have
no default.

## Advanced

The kwargs-and-env path covers most apps. When you need more, the
[Advanced configuration](advanced/config.md) page covers:

- Building from a Pydantic config object with `from_config`.
- Composing settings under one `pydantic-settings` tree.
- Custom env prefixes with `env_prefix=` and disabling env reads with
  `env_load=False`.
- The full resolution order contract.

For live reload from a Kubernetes ConfigMap, see
[Live reconfiguration](configuration/reconfigure-from-configmap.md).
