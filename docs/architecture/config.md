# Configuration

This document specifies how grelmicro components are configured. It defines the contract all components follow, the resolution rules between kwargs, explicit config objects, and environment variables, and the reasoning behind the design.

## Goals

1. **Library, not application.** grelmicro ships typed Pydantic config classes with clean field names. Config classes themselves carry no environment bindings. Components expose an opt-in Environmental path that reads from a narrow, grelmicro-scoped env namespace (`GREL_*`) which the app can always override or disable.
2. **Three complete paths.** Every component can be configured programmatically (kwargs), declaratively (a pre-built `Config` instance), or environmentally (12-factor). All three reach the same validated state.
3. **One rule across components.** The resolution order is identical for every component. Learn it once, apply everywhere.
4. **Identity stays visible.** For multi-instance components (lock, rate limiter, leader election), the instance name is always a required positional argument. It is grep-friendly and never hidden inside a config object.
5. **Runtime reconfiguration is not blocked.** The design keeps `self._config` as a single Pydantic pointer so an atomic swap remains feasible in the future.
6. **Hot path is untouched.** All merging, env reading, and validation happens once at construction. Runtime reads stay on the Pydantic model, consistent with the `RateLimitFilter.filter` benchmark showing per-field access costs ~2 ns and accounts for ~1% of a 255 ns call.

## The one rule — resolution order

For each component, the final config is built from these sources, top wins on conflict:

1. **kwargs** passed to the component constructor (most explicit).
2. **`config=`** — a pre-built `Config` instance. When present, sources 1 and 3 are ignored.
3. **Environment variables** matching the component's derived prefix.
4. **Defaults** declared on the `Config` class (least explicit).

Mixing `config=` with any other config-valued kwarg raises `TypeError`. `backend=` and identity kwargs (`name`) are always allowed alongside `config=` since they are not part of the serializable config.

## The three paths

| Path | Call | When to use |
|------|------|-------------|
| **Programmatic** | `Lock("cart", lease_duration=60)` | Scripts, notebooks, and code-first setups where all values are known inline. |
| **Declarative** | `Lock("cart", config=cfg)` | Production where a settings tree is assembled at startup (YAML, Vault, central settings). |
| **Environmental** | `Lock("cart")` | Zero-boilerplate 12-factor deployments. Fields resolve from env, fall back to defaults. |

A fourth shape is the layered override — `Lock("cart", config=base_cfg, lease_duration=30)` — which is rejected. If you want a config baseline with kwarg overrides, derive a new config via `base_cfg.model_copy(update={"lease_duration": 30})` and pass that.

## Name as namespace

Multi-instance components derive their environment prefix from the component name and the positional instance name:

```
GREL_{COMPONENT}_{NAME_UPPER}_{FIELD_UPPER}
```

Examples:

```
GREL_LOCK_CART_LEASE_DURATION=60
GREL_LOCK_PAYMENTS_LEASE_DURATION=120
GREL_RATE_LIMITER_API_ALGORITHM__LIMIT=5000
GREL_TASK_LOCK_CLEANUP_MAX_LOCK_SECONDS=300
GREL_LEADER_ELECTION_CRON_RETRY_INTERVAL=5
```

Single-instance components drop the instance-name segment:

```
GREL_HEALTH_CACHE_TTL=2.0
GREL_LOG_LEVEL=DEBUG
GREL_RATE_LIMIT_FILTER_CAPACITY=50
GREL_DUPLICATE_FILTER_CACHE_SIZE=1024
```

This is a deliberate parallel to Spring Boot's `@ConfigurationProperties(prefix="myapp.locks.cart")`, with the prefix derived from runtime identity rather than an annotation.

### Prefix reference

| Component | Auto-derived prefix |
|-----------|---------------------|
| `Lock` | `GREL_LOCK_{NAME_UPPER}_` |
| `TaskLock` | `GREL_TASK_LOCK_{NAME_UPPER}_` |
| `RateLimiter` | `GREL_RATE_LIMITER_{NAME_UPPER}_` |
| `LeaderElection` | `GREL_LEADER_ELECTION_{NAME_UPPER}_` |
| `RateLimitFilter` | `GREL_RATE_LIMIT_FILTER_` |
| `DuplicateFilter` | `GREL_DUPLICATE_FILTER_` |
| `HealthRegistry` | `GREL_HEALTH_` |
| `LoggingSettings` | `GREL_LOG_` |

The `GREL_` prefix makes grelmicro ownership explicit, avoids collision with unrelated environment variables, and follows the same pattern as `UVICORN_*`, `GUNICORN_*`, `CELERY_*`, `DJANGO_*`. Apps that want their own convention pass `env_prefix=` explicitly on construction.

## Positional name is authoritative

When both a positional `name` and a `config` with a `name` field are present:

| Positional `name` | `config.name` | Behaviour |
|-------------------|---------------|-----------|
| `"cart"` | `"cart"` | Match. Use config as is. |
| `"cart"` | unset | Fill `config.name` from the positional. |
| `"cart"` | `"payments"` | `ValueError` on the mismatch. |

The positional wins because it is the identity argument, not a config field. This keeps call sites readable and lets YAML dict keys act as implicit names.

## Env prefix override and escape hatch

Two constructor kwargs customise env behaviour:

- **`env_prefix: str | None = None`** — override the auto-derived prefix. Use when the app wants its own convention, for example `MYAPP_LOCK_CART_*` instead of `GREL_LOCK_CART_*`.
- **`read_env: bool = True`** — disable env reading entirely. Use when the caller wants to guarantee the environment has no influence on construction, for example when every field is already supplied via kwargs or via an explicit `config=`.

Example:

```python
Lock("cart", env_prefix="MYAPP_LOCK_CART_")     # reads MYAPP_LOCK_CART_*
Lock("cart", read_env=False, lease_duration=10) # env ignored entirely
```

## What grelmicro ships, what the app ships

| Layer | grelmicro | The application |
|-------|-----------|-----------------|
| Pydantic `Config` classes (`LockConfig`, `RateLimiterConfig`, ...) | yes, no env bindings | no |
| Component constructors that accept kwargs, `config=`, or env | yes | no |
| `GREL_*` as the default env namespace for the Environmental path | yes | no |
| Docs showing YAML + pydantic-settings recipes | yes | no |
| `AppSettings(BaseSettings)` wrapper | no | yes |
| Alternative env prefix (e.g. `MYAPP_*`) | no | yes (via `env_prefix=`) |
| YAML path, `.env`, Vault, Consul, etc. | no | yes (via pydantic-settings or another loader) |
| Instance naming (`cart`, `payments`) | no | yes |

`GREL_*` is the **default** when the Environmental path is used. It is not a claim on any other namespace, and `env_prefix=` always lets the app pick its own. The Config classes themselves stay env-free so they compose freely into the app's own settings tree.

## Public API surface per component

Every component follows the same shape:

```python
class Component:
    @overload
    def __init__(
        self,
        name: str,
        *,
        config: ComponentConfig,
        backend: Backend | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self,
        name: str,
        *,
        backend: Backend | None = None,
        field_a: ... | None = None,
        field_b: ... | None = None,
        env_prefix: str | None = None,
        read_env: bool = True,
    ) -> None: ...
```

The runtime `__init__` enforces mutual exclusion, derives `env_prefix` when not supplied, and delegates to a shared `resolve_config` helper.

## Single-instance variant

Single-instance components drop the positional `name` but follow the same three paths. Grelmicro exposes a pure Pydantic `Config` and an opt-in convenience `Settings` subclass that adds the `GREL_*` env prefix:

```python
# canonical, no env
class LoggingConfig(BaseModel):
    backend: LoggingBackendType = LoggingBackendType.STDLIB
    level: LoggingLevelType = LoggingLevelType.INFO
    format: LoggingFormatType | str = LoggingFormatType.AUTO
    # ...

# opt-in convenience: GREL_LOG_* env vars
class LoggingSettings(LoggingConfig, BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GREL_LOG_")
```

Apps can bypass the convenience subclass and pick their own prefix:

```python
class MyLoggingSettings(LoggingConfig, BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYAPP_LOG_")
```

Field names on every `Config` stay lowercase and PEP 8 compliant. The `env_prefix` on the opt-in `Settings` subclass produces the uppercase env vars. This keeps Python code readable (`settings.level`, not `settings.LOG_LEVEL`) while the env stays ops-friendly (`GREL_LOG_LEVEL`).

## Runtime reconfiguration (future)

The current design keeps `self._config` as a single immutable Pydantic model assigned in one step. This preserves a future extension:

```python
def reconfigure(self, config: ComponentConfig) -> None:
    """Atomically replace the config. In-flight operations started
    before this call continue with the prior snapshot."""
    self._config = config
```

Atomicity holds because Python attribute assignment is atomic under the GIL and the config is a single pointer. The field-copy pattern (one instance attribute per config field) would forfeit this guarantee and was explicitly rejected (see Decisions below).

## Decisions and alternatives rejected

### Field-copy pattern (issue #113)
Copy each config field to a plain instance attribute at construction time to shave ~2 ns per field read. Rejected because:

- Benchmarks show ~1% of call time in the tightest synchronous hot path (`RateLimitFilter.filter`) and zero measurable impact on network-backed paths (lock, rate limiter).
- Breaks atomic-swap for future `reconfigure()` — multiple pointer swaps, torn reads possible.
- Adds 43 duplication sites that must stay in sync on every new field.

### Drop Pydantic in favour of `@dataclass(frozen=True, slots=True)`
Loses `PositiveFloat`, `PositiveInt`, and `@model_validator` constraints. The grelmicro style is consistently Pydantic. The inconsistency cost outweighs the ~1 ns per field saved.

### Uppercase field names on Config classes (today's `LoggingSettings`)
Today's `LoggingSettings` bakes field names like `LOG_BACKEND` to match env var names directly. Rejected going forward because:

- Python access is non-idiomatic (`settings.LOG_LEVEL` violates PEP 8).
- Programmatic construction reads as `LoggingSettings(LOG_LEVEL=...)` which is awkward.
- Field names cannot encode runtime instance names, so the pattern breaks entirely for multi-instance components (`GREL_LOCK_CART_LEASE_DURATION` cannot be a class field).

The replacement is lowercase Config field names + `env_prefix` on an opt-in `Settings` subclass. `LoggingSettings` migrates to this shape in step 5 of the rollout plan with a deprecation window for `LOG_*` env vars.

### Bare env prefix (`LOCK_*`, `LOG_*`) with no namespace
Rejected because `LOCK_*`, `LOG_*`, and similar collide with conventions other libraries and apps already use. `GREL_*` scopes grelmicro's defaults to a distinct namespace, matching `UVICORN_*`, `GUNICORN_*`, `CELERY_*`, `DJANGO_*`.

### No library default prefix (force users to always supply `env_prefix=`)
Rejected because it eliminates the Environmental path. Zero-config 12-factor deployments need *some* default. `GREL_*` is that default, always overridable via `env_prefix=` and always disableable via `read_env=False`.

## Relationship to related work

- **Spring Boot** — `@ConfigurationProperties(prefix="myapp.locks.cart")` declares the prefix on a bean class. grelmicro derives the prefix from the positional identity argument instead.
- **FastAPI** — ships `Request`, `Response`, `APIRouter` and leaves application settings to the app. grelmicro follows the same split for Config classes. The opt-in Environmental path is the one pragmatic deviation.
- **uvicorn / gunicorn / celery / django** — ship `UVICORN_*`, `GUNICORN_*`, `CELERY_*`, `DJANGO_*` env namespaces for their own settings. grelmicro's `GREL_*` is the same pattern.
- **pydantic-settings** — carries the env, file, and secrets loading story. grelmicro depends on the existing project dependency and adds no loader code of its own.
