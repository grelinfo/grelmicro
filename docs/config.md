# Configuration

Every config-shaped grelmicro component takes its settings the same way. Pick the path that matches how your application is wired:

| Path | Call | When to use |
|---|---|---|
| **Programmatic** | `Lock("cart", lease_duration=60)` or `RateLimiter.token_bucket("api", capacity=10, refill_rate=1)` | Scripts, notebooks, and code-first setups where all values are known inline. |
| **Environmental** | `Lock("cart")` | Zero-boilerplate 12-factor deployments. Fields resolve from env, fall back to defaults. |
| **Declarative** | `Lock.from_config("cart", cfg)` or `RateLimiter.from_config("api", cfg)` | Production where a settings tree is assembled at startup from YAML, Vault, or any central source. |

The three paths share one resolution rule: caller `**kwargs` win, then env, then defaults. `None` kwargs are treated as unset and fall through to the next layer.

## Programmatic

Pass values inline:

```python
from grelmicro.sync import Lock

lock = Lock("cart", lease_duration=60, retry_interval=0.1)
```

For variant-driven components (`RateLimiter`), use the factory classmethods:

```python
from grelmicro.resilience import RateLimiter

api_limiter = RateLimiter.token_bucket("api", capacity=100, refill_rate=10)
auth_limiter = RateLimiter.gcra("auth", limit=5, window=60)
```

## Environmental

Set env vars under the component's prefix and call the constructor with just the name:

```bash
export GREL_LOCK_CART_LEASE_DURATION=60
export GREL_LOCK_CART_RETRY_INTERVAL=0.1
```

```python
lock = Lock("cart")  # reads GREL_LOCK_CART_*
```

The instance name (`"cart"`) becomes the namespace inside the prefix. Names with hyphens, dots, slashes, or colons normalise into uppercase POSIX segments (`payments-eu` → `PAYMENTS_EU`, `cart.v2` → `CART_V2`).

### Prefix reference

| Component | Prefix |
|---|---|
| `Lock("cart")` | `GREL_LOCK_CART_` |
| `TaskLock("etl")` | `GREL_TASK_LOCK_ETL_` |
| `LeaderElection("svc")` | `GREL_LEADER_ELECTION_SVC_` |
| `RateLimitFilter()` | `GREL_RATE_LIMIT_FILTER_` |
| `DuplicateFilter()` | `GREL_DUPLICATE_FILTER_` |
| `HealthChecks()` | `GREL_HEALTH_` |
| `log.configure()` | `GREL_LOG_` |

## Declarative

Build a config object, then construct via `from_config`:

```python
from grelmicro.sync import Lock
from grelmicro.sync.lock import LockConfig

cfg = LockConfig(lease_duration=60, retry_interval=0.1)
lock = Lock.from_config("cart", cfg)
```

The config object is a frozen Pydantic model. Field names match the kwargs from the programmatic path. `from_config` skips the env layer entirely.

## Resolution order

When `__init__` runs, the final value of each field is picked from the first source that has it:

1. Caller `**kwargs`.
2. Env var matching the component prefix (only when `read_env=True`).
3. `Config` class default.

## Recipes

### Custom env prefix

```python
lock = Lock("cart", env_prefix="MYAPP_LOCK_CART_")
```

### Disable env reads

```python
lock = Lock("cart", read_env=False, lease_duration=10)
```

### Wire from `pydantic-settings`

Centralise everything under one `BaseSettings` and hand grelmicro the slices it needs:

```python
from pydantic_settings import BaseSettings
from grelmicro.sync import Lock
from grelmicro.sync.lock import LockConfig
from grelmicro.cache.redis import RedisCacheAdapter

class AppSettings(BaseSettings):
    cart_lock: LockConfig = LockConfig()
    redis_url: str = "redis://localhost:6379/0"

settings = AppSettings()
cart_lock = Lock.from_config("cart", settings.cart_lock)
cache_backend = RedisCacheAdapter(settings.redis_url)
```

## Going deeper

The [Configuration architecture](architecture/config.md) page covers `resolve_config()`, hot-path discipline, and where the `Config` classes live.
