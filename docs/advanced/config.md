# Declarative Configuration

The [User Guide Configuration](../config.md) page teaches the default path: build
with keyword arguments, tune with environment variables. This page covers the
other two construction paths and the resolution contract behind all three.

Every config-shaped grelmicro component takes its settings the same way. Pick the
path that matches how your application is wired:

| Path | Call | When to use |
|---|---|---|
| **Programmatic** | `Lock("cart", lease_duration=60)` or `RateLimiter.token_bucket("api", capacity=10, refill_rate=1)` | Scripts, notebooks, and code-first setups where all values are known inline. |
| **Environmental** | `Lock("cart")` | Zero-boilerplate 12-factor deployments. Fields resolve from env, fall back to defaults. |
| **Declarative** | `Lock.from_config("cart", cfg)` or `RateLimiter.from_config("api", cfg)` | Production where a settings tree is assembled at startup from YAML, Vault, or any central source. |

The three paths share one resolution rule: caller `**kwargs` win, then env, then
defaults. `None` kwargs are treated as unset and fall through to the next layer.

## Programmatic

Pass values inline:

```python
from grelmicro.coordination import Lock

lock = Lock("cart", lease_duration=60, retry_interval=0.1)
```

For variant-driven components (`RateLimiter`), use the factory classmethods:

```python
from grelmicro.resilience import RateLimiter

api_limiter = RateLimiter.token_bucket("api", capacity=100, refill_rate=10)
auth_limiter = RateLimiter.sliding_window("auth", limit=5, window=60)
```

## Environmental

Set env vars under the component's prefix and call the constructor with just the
name:

```bash
export GREL_LOCK_CART_LEASE_DURATION=60
export GREL_LOCK_CART_RETRY_INTERVAL=0.1
```

```python
lock = Lock("cart")  # reads GREL_LOCK_CART_*
```

The instance name (`"cart"`) becomes the namespace inside the prefix. Names with
hyphens, dots, slashes, or colons normalise into uppercase POSIX segments
(`payments-eu` becomes `PAYMENTS_EU`, `cart.v2` becomes `CART_V2`).

The default instance drops the name segment, so a `Lock("default")` reads the
bare `GREL_LOCK_*`. The default instance owns the bare `GREL_{COMPONENT}_`
namespace, so name your other instances to avoid clashing with a field name (a
`Lock("lease")` would share `GREL_LOCK_LEASE_DURATION` with the default
instance). This is rare in practice.

### Prefix reference

| Component | Prefix |
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

## Declarative

Build a config object, then construct via `from_config`:

```python title="lock_declarative.py"
--8<-- "coordination/lock_declarative.py"
```

The config object is a frozen Pydantic model. Field names match the kwargs from
the programmatic path. `from_config` skips the env layer entirely.

Every primitive takes the same path. Some accept the config as a `config=`
keyword instead of `from_config`, because their config type also selects the
algorithm:

=== "Retry"
    ```python
    --8<-- "resilience/retry_declarative.py"
    ```

=== "Timeout"
    ```python
    --8<-- "resilience/timeout_declarative.py"
    ```

=== "Shield"
    ```python
    --8<-- "resilience/shield_declarative.py"
    ```

=== "Fallback"
    ```python
    --8<-- "resilience/fallback_declarative.py"
    ```

=== "Circuit breaker"
    ```python
    --8<-- "resilience/circuitbreaker_declarative.py"
    ```

=== "Rate limiter"
    ```python
    --8<-- "resilience/ratelimiter_from_config.py"
    ```

## Resolution order

When `__init__` runs, the final value of each field is picked from the first
source that has it:

1. Caller `**kwargs`.
2. Env var matching the component prefix (when `env_load=True`, or when
   `env_load` is unset and `GREL_ENV_LOAD` is truthy).
3. `Config` class default.

### The hazard in step 2

Step 2 fills every field the caller did not pass, not only the fields that have
no default anywhere. Passing some fields and leaving others therefore splits the
config across two sources.

That is the trap for config held in your own `Settings` object. The fields you
hand over win, and the rest come from the environment. A `Settings` default that
differs from the environment is dropped, and nothing says so.

```python
class AppSettings(BaseSettings):
    lease: float = 30.0  # your default


settings = AppSettings()
lock = Lock("cart", retry_interval=0.5)  # lease_duration not passed
```

With `GREL_LOCK_CART_LEASE_DURATION=99` in the environment, that lock leases for
99 seconds. Not 30, and not the library default either. The failure is silent
and only shows up when the two sources disagree, which is the case nobody
tests.

## Recipes

### Custom env prefix

```python
lock = Lock("cart", env_prefix="MYAPP_LOCK_CART_")
```

### Disable env reads

```python
lock = Lock("cart", env_load=False, lease_duration=10)
```

`env_load=False` says the values passed here are the whole truth. Every field
not passed falls back to the `Config` default, and step 2 is skipped, so the
environment cannot fill the gaps.

Reach for it whenever the values come from somewhere that is already the source
of truth. `from_config` says the same thing positively and reads better when the
config already exists as an object.

### Wire from `pydantic-settings`

Centralise everything under one `BaseSettings` and hand grelmicro the slices it
needs:

```python
from pydantic_settings import BaseSettings

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.providers.redis import RedisProvider
from grelmicro.coordination import Lock
from grelmicro.coordination.lock import LockConfig

class AppSettings(BaseSettings):
    cart_lock: LockConfig = LockConfig()
    redis_url: str = "redis://localhost:6379/0"

settings = AppSettings()
cart_lock = Lock.from_config("cart", settings.cart_lock)
redis = RedisProvider(settings.redis_url)
micro = Grelmicro(uses=[Cache(redis)])
```

## Going deeper

The [Configuration architecture](../architecture/config.md) page covers
`resolve_config()`, hot-path discipline, and where the `Config` classes live.
