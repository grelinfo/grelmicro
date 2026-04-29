# Configuration

This document specifies how grelmicro components are configured. It defines the contract all components follow, the resolution rules between kwargs, explicit config objects, and environment variables, and the reasoning behind the design.

## Goals

1. **Library, not application.** grelmicro ships typed Pydantic config classes with clean field names. Config classes themselves carry no environment bindings. Components expose an opt-in Environmental path that reads from a narrow, grelmicro-scoped env namespace (`GREL_*`) which the app can always override or disable.
2. **Small, explicit paths.** Config-shaped components expose programmatic, declarative, and environmental paths. Variant-driven components such as `RateLimiter` expose simple factory methods for programmatic use and keep config objects for declarative composition.
3. **One rule across components.** The resolution order is identical for every component. Learn it once, apply everywhere.
4. **Identity stays visible.** For multi-instance components (lock, rate limiter, leader election), the instance name is always a required positional argument. It is grep-friendly and never hidden inside a config object.
5. **Runtime reconfiguration is not blocked.** The design keeps `self._config` as a single Pydantic pointer so an atomic swap remains feasible in the future.
6. **Hot path is untouched.** All merging, env reading, and validation happens once at construction. Runtime reads stay on the Pydantic model, consistent with the `RateLimitFilter.filter` benchmark showing per-field access costs ~2 ns and accounts for ~1% of a 255 ns call.

## Construction shapes

Grelmicro uses two public patterns, chosen per component:

- **Config-shaped components** such as `Lock` and `TaskLock`: `Component(name, **kwargs)` for code-first and environmental construction, plus `Component.from_config(name, config)` for declarative composition.
- **Variant-driven components** such as `RateLimiter`: factory classmethods for the simple Python path, plus `Component.from_config(name, config)` for declarative composition.

The `Config` classes carry settings only. The instance identity lives on the component itself. This matches the `Map<String, Settings>` shape used by every major declarative-config framework.

### Resolution inside `__init__`

When a config-shaped component constructs via `__init__`, the final config merges these sources, top wins:

1. **kwargs** from the caller (explicit values).
2. **Environment variables** matching the component's derived prefix (only when `read_env=True`).
3. **Defaults** declared on the `Config` class.

`None` kwarg values are treated as unset. They fall through to the env or default layers.

### When to use each path

| Path | Call | When to use |
|------|------|-------------|
| **Programmatic** | `Lock("cart", lease_duration=60)` or `RateLimiter.token_bucket("api", capacity=10, refill_rate=1)` | Scripts, notebooks, and code-first setups where all values are known inline. |
| **Environmental** | `Lock("cart")` | Zero-boilerplate 12-factor deployments for config-shaped components. Fields resolve from env, fall back to defaults. |
| **Declarative** | `Lock.from_config("cart", cfg)` or `RateLimiter.from_config("api", cfg)` | Production where a settings tree is assembled at startup from YAML, Vault, or any central source. |

The first two share `__init__`. Only their inputs differ. The declarative path is the classmethod.

A layered shape (kwarg overrides on top of an explicit config) is **not** offered. If you want a config baseline with overrides, build the new config explicitly: `cfg2 = cfg.model_copy(update={"lease_duration": 30})`, then `Lock.from_config("cart", cfg2)`.

## Name as namespace

Multi-instance components derive their environment prefix from the component name and the positional instance name:

```
GREL_{COMPONENT}_{NAME_UPPER}_{FIELD_UPPER}
```

Examples:

```
GREL_LOCK_CART_LEASE_DURATION=60
GREL_LOCK_PAYMENTS_LEASE_DURATION=120
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
| `LeaderElection` | `GREL_LEADER_ELECTION_{NAME_UPPER}_` |
| `RateLimitFilter` | `GREL_RATE_LIMIT_FILTER_` |
| `DuplicateFilter` | `GREL_DUPLICATE_FILTER_` |
| `HealthRegistry` | `GREL_HEALTH_` |
| `log.configure` | `GREL_LOG_` |

The `GREL_` prefix makes grelmicro ownership explicit, avoids collision with unrelated environment variables, and follows the same pattern as `UVICORN_*`, `GUNICORN_*`, `CELERY_*`, `DJANGO_*`. Apps that want their own convention pass `env_prefix=` explicitly on construction.

### Name normalisation

Instance names are normalised before they enter an env prefix so that natural identifiers like `payments-eu`, `cart.v2`, or `weather/svc` produce valid POSIX environment variables. The rule, implemented as `grelmicro._config.env_segment`:

- Upper-case the name.
- Replace any character outside `[A-Z0-9_]` with `_`.
- Collapse runs of underscores into one.
- Strip leading and trailing underscores.

| Name | Env segment |
|------|-------------|
| `cart` | `CART` |
| `payments-eu` | `PAYMENTS_EU` |
| `cart.v2` | `CART_V2` |
| `foo:bar` | `FOO_BAR` |
| `weather/svc` | `WEATHER_SVC` |
| `svc:prod-1` | `SVC_PROD_1` |

The rule is conservative: a name with no portable characters or a result that starts with a digit is rejected at construction with a clear error. Apps that need a different mapping pass `env_prefix=` explicitly to bypass derivation.

## Identity belongs in the call, not in the config

`Config` classes carry settings only. Identity is the positional `name` on the component. YAML and env aggregations key by name naturally:

```yaml
locks:
  cart:    { lease_duration: 60 }
  payments: { lease_duration: 120 }
```

```python
locks = {n: Lock.from_config(n, cfg) for n, cfg in settings.locks.items()}
```

```bash
GREL_LOCKS__CART__LEASE_DURATION=60
```

No redundant `name: cart` field in YAML, no `*_NAME=cart` env var to populate.

## Env prefix override and escape hatch

Two constructor kwargs customise env behaviour on components that expose the Environmental path:

- **`env_prefix: str | None = None`**: override the auto-derived prefix. Use when the app wants its own convention, for example `MYAPP_LOCK_CART_*` instead of `GREL_LOCK_CART_*`.
- **`read_env: bool = True`**: disable env reading entirely. Use when the caller wants to guarantee the environment has no influence on construction, for example when every field is already supplied via kwargs or when construction happens via `from_config(...)`.

Example:

```python
Lock("cart", env_prefix="MYAPP_LOCK_CART_")     # reads MYAPP_LOCK_CART_*
Lock("cart", read_env=False, lease_duration=10) # env ignored entirely
```

## What grelmicro ships, what the app ships

| Layer | grelmicro | The application |
|-------|-----------|-----------------|
| Pydantic `Config` classes (`LockConfig`, `RateLimiterConfig`, ...) | yes, no env bindings | no |
| Component entry points for programmatic use, plus `from_config(...)` for declarative composition | yes | no |
| `GREL_*` as the default env namespace for the Environmental path | yes | no |
| Docs showing YAML + pydantic-settings recipes | yes | no |
| `AppSettings(BaseSettings)` wrapper | no | yes |
| Alternative env prefix (e.g. `MYAPP_*`) | no | yes (via `env_prefix=`) |
| YAML path, `.env`, Vault, Consul, etc. | no | yes (via pydantic-settings or another loader) |
| Instance naming (`cart`, `payments`) | no | yes |

`GREL_*` is the **default** when the Environmental path is used. It is not a claim on any other namespace, and `env_prefix=` always lets the app pick its own. The Config classes themselves stay env-free so they compose freely into the app's own settings tree.

## Backend resolution

Components do not look up their backend during construction. The
registry call is deferred to the first method that actually needs
the backend, and the result is cached on the instance.

```python
class Lock:
    def __init__(self, name, *, backend=None, ...):
        ...
        self._backend: SyncBackend | None = backend  # may be None

    @property
    def backend(self) -> SyncBackend:
        return self._backend or self._resolve_backend()

    def _resolve_backend(self) -> SyncBackend:
        backend = get_sync_backend()
        self._backend = backend
        return backend
```

Internal hot-path methods read through the same short-circuit:

```python
backend = self._backend or self._resolve_backend()
return await backend.acquire(...)
```

Three properties hold:

- **Construction is pure.** `Lock("cart")` performs no registry
  call. `BackendNotLoadedError` only ever surfaces on the first
  operation, never at import or construction time.
- **Cached after first use.** Resolution is `O(1)` per call. The
  first call writes `self._backend`, subsequent calls hit the
  attribute directly. Measured cost: about 33 ns per access in
  steady state.
- **Public `backend` property.** Each component exposes a
  read-only `backend` property so callers and tests can introspect
  the bound backend without reaching for private state.

The same shape applies to `RateLimiter`, where a second lazy step
binds the algorithm config into a strategy:

```python
@property
def backend(self) -> RateLimiterBackend:
    return self._backend or self._resolve_backend()

def _resolve_strategy(self) -> RateLimiterStrategy:
    strategy = self.backend.bind(self._config)
    self._strategy = strategy
    return strategy
```

The hot-path methods read `self._strategy or self._resolve_strategy()`
so the bind step is paid exactly once and the strategy method is
called directly thereafter, preserving the
"resolve the choice once, forward directly on every call" rule
from `CONTRIBUTING.md`.

## Variants: one class with factories vs separate components

Some primitives have variants. The rule for shaping the API:

- **Interchangeable variants** (same public interface, same observable behaviour, only the internals differ): one class. Variants are factory classmethods, named after the discriminator value.
- **Distinct variants** (different semantics, callers must know which one they hold): separate classes. Each variant is its own component.

### Interchangeable: one class, factory classmethods

A token bucket and a GCRA rate limiter both expose `acquire`, `peek`, `reset`. Swapping the algorithm does not require changing the caller. The choice is a tuning parameter.

```python
RateLimiter.token_bucket("api", capacity=10, refill_rate=1)
RateLimiter.gcra("auth", limit=5, window=60)
```

One class (`RateLimiter`), one config-typed declarative path (`RateLimiter.from_config(name, cfg)`), and one factory classmethod per variant.

### Distinct: separate classes

A reentrant lock allows the same task to acquire it twice. A regular lock raises on the second acquire. Swapping them silently breaks correctness, so the caller must hold the right type.

```python
Lock("cart", lease_duration=60)
SpinLock("config", max_spins=100)
ReentrantLock("recursive", lease_duration=60)
```

Each is its own class with its own `__init__`, its own `from_config`, and its own type. The type checker stops a `ReentrantLock` from being used where a `Lock` is expected.

### Quick test

> Can a caller use one variant where another was specified, without touching the call site?

- **Yes**: interchangeable. One class with factory classmethods.
- **No**: distinct. Separate classes.

This rule is what tells `RateLimiter.token_bucket(...)` apart from `ReentrantLock(...)`.

## Public API surface

Config-shaped components follow one clear shape: a single `__init__` for the kwargs-and-env path plus a `from_config` classmethod for the declarative path.

```python
class Component:
    def __init__(
        self,
        name: str,
        *,
        backend: Backend | None = None,
        field_a: ... | None = None,
        field_b: ... | None = None,
        env_prefix: str | None = None,
        read_env: bool = True,
    ) -> None:
        """Construct from kwargs, optionally consulting environment variables."""

    @classmethod
    def from_config(
        cls,
        name: str,
        config: ComponentConfig,
        *,
        backend: Backend | None = None,
    ) -> Self:
        """Construct from a name and a pre-built Config. Bypasses kwargs and env."""
```

`__init__` derives `env_prefix` when not supplied and delegates to the shared `resolve_config` helper. `from_config` wires the instance directly via `cls.__new__` plus a private `_setup` helper. Neither path needs `@overload` stubs because the two are physically separate methods.

Variant-driven components keep the same declarative entry point but use factories for the simple Python path:

```python
class VariantComponent:
    @classmethod
    def token_bucket(cls, name: str, *, capacity: int, refill_rate: float) -> Self:
        ...

    @classmethod
    def gcra(cls, name: str, *, limit: int, window: float) -> Self:
        ...

    @classmethod
    def from_config(cls, name: str, config: ComponentConfig) -> Self:
        ...
```

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

Field names on every `Config` stay lowercase and PEP 8 compliant. The `env_prefix` on the opt-in `Settings` subclass produces the uppercase env vars. This keeps Python code readable (`settings.level`, not `settings.GREL_LOG_LEVEL`) while the env stays ops-friendly (`GREL_LOG_LEVEL`).

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
- Breaks atomic-swap for future `reconfigure()`. Multiple pointer swaps means torn reads are possible.
- Adds 43 duplication sites that must stay in sync on every new field.

### Drop Pydantic in favour of `@dataclass(frozen=True, slots=True)`
Loses `PositiveFloat`, `PositiveInt`, and `@model_validator` constraints. The grelmicro style is consistently Pydantic. The inconsistency cost outweighs the ~1 ns per field saved.

### Uppercase field names on Config classes (today's `LoggingSettings`)
Today's `LoggingSettings` bakes field names like `GREL_LOG_BACKEND` to match env var names directly. Rejected going forward because:

- Python access is non-idiomatic (`settings.GREL_LOG_LEVEL` violates PEP 8).
- Programmatic construction reads as `LoggingSettings(GREL_LOG_LEVEL=...)` which is awkward.
- Field names cannot encode runtime instance names, so the pattern breaks entirely for multi-instance components (`GREL_LOCK_CART_LEASE_DURATION` cannot be a class field).

The replacement is lowercase Config field names + `env_prefix` on an opt-in `Settings` subclass. `LoggingSettings` migrates to this shape in step 5 of the rollout plan with a deprecation window for `LOG_*` env vars.

### Bare env prefix (`LOCK_*`, `LOG_*`) with no namespace
Rejected because `LOCK_*`, `LOG_*`, and similar collide with conventions other libraries and apps already use. `GREL_*` scopes grelmicro's defaults to a distinct namespace, matching `UVICORN_*`, `GUNICORN_*`, `CELERY_*`, `DJANGO_*`.

### No library default prefix (force users to always supply `env_prefix=`)
Rejected because it eliminates the Environmental path. Zero-config 12-factor deployments need *some* default. `GREL_*` is that default, always overridable via `env_prefix=` and always disableable via `read_env=False`.

## Relationship to related work

- **Pydantic**: `Model(**data)` and `Model.model_validate(data)` are the two canonical construction paths. grelmicro mirrors that split with `Component(name, **kwargs)` and `Component.from_config(name, cfg)`, keeping each path single-purpose.
- **Spring Boot**: `@ConfigurationProperties(prefix="myapp.locks.cart")` declares the prefix on a bean class. grelmicro derives the prefix from the positional identity argument instead.
- **FastAPI**: ships `Request`, `Response`, `APIRouter` with single-signature `__init__` plus rich `Annotated` types, no `@overload` stubs. grelmicro follows the same approach: one signature per method, `Annotated[..., Doc(...)]` for parameter docs.
- **uvicorn / gunicorn / celery / django**: ship `UVICORN_*`, `GUNICORN_*`, `CELERY_*`, `DJANGO_*` env namespaces for their own settings. grelmicro's `GREL_*` is the same pattern.
- **pydantic-settings**: carries the env, file, and secrets loading story. grelmicro depends on the existing project dependency and adds no loader code of its own.
