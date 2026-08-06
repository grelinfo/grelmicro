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
gate a [Provider](providers.md), and it never did. `RedisProvider()` reads
`REDIS_URL`, `PostgresProvider()` reads `POSTGRES_URL`, with no flag, because
those names belong to your environment rather than to grelmicro. A provider
variable that is missing fails at construction and names the variable it
wanted, so there is no silent default to protect you from. Pass
`env_load=False` to a provider to turn its env reads off.

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

The instance name becomes the namespace. Names with hyphens, dots, slashes, or
colons normalise into uppercase segments (`payments-eu` becomes `PAYMENTS_EU`,
`cart.v2` becomes `CART_V2`).

The default instance drops the name segment, so a `Lock("default")` reads the
bare `GREL_LOCK_LEASE_DURATION`. Because the default instance owns the bare
`GREL_{PATTERN}_` namespace, name your other instances to avoid clashing with a
field name (a `Lock("lease")` would share `GREL_LOCK_LEASE_DURATION` with the
default instance). This is rare in practice.

A value passed in code wins over the environment. So a hard-coded
`Lock("cart", lease_duration=60)` ignores `GREL_LOCK_CART_LEASE_DURATION`. Leave
a field out of the constructor to let the deployment set it.

### Prefix reference

| Pattern | Prefix |
|---|---|
| `Lock("default")` | `GREL_LOCK_` |
| `Lock("cart")` | `GREL_LOCK_CART_` |
| `TaskLock("etl")` | `GREL_TASKLOCK_ETL_` |
| `LeaderElection("svc")` | `GREL_LEADERELECTION_SVC_` |
| `RateLimitFilter()` | `GREL_RATELIMITFILTER_` |
| `RateLimitFilter(env_name="audit")` | `GREL_RATELIMITFILTER_AUDIT_` |
| `DuplicateFilter()` | `GREL_DUPLICATEFILTER_` |
| `DuplicateFilter(env_name="audit")` | `GREL_DUPLICATEFILTER_AUDIT_` |
| `HealthChecks()` | `GREL_HEALTH_` |
| `log.configure()` | `GREL_LOG_` |

Each pattern page lists its own fields and the exact variable names.

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
