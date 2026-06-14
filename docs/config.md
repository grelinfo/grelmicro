# Configuration

You build a pattern with keyword arguments. You tune it in deployment with
environment variables. No code change between the two.

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

A value passed in code wins over the environment. So a hard-coded
`Lock("cart", lease_duration=60)` ignores `GREL_LOCK_CART_LEASE_DURATION`. Leave
a field out of the constructor to let the deployment set it.

### Prefix reference

| Pattern | Prefix |
|---|---|
| `Lock("cart")` | `GREL_LOCK_CART_` |
| `TaskLock("etl")` | `GREL_TASKLOCK_ETL_` |
| `LeaderElection("svc")` | `GREL_LEADERELECTION_SVC_` |
| `RateLimitFilter()` | `GREL_RATE_LIMIT_FILTER_` |
| `DuplicateFilter()` | `GREL_DUPLICATE_FILTER_` |
| `HealthChecks()` | `GREL_HEALTH_` |
| `log.configure()` | `GREL_LOG_` |

Each pattern page lists its own fields and the exact variable names.

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
