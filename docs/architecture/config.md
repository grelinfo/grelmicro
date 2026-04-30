# Configuration internals

This page is the engineering side of [Configuration](../config.md). It assumes you already know the three paths and the resolution order. It documents the machinery that makes them deterministic and cheap.

## The contract

Every config-shaped component (`Lock`, `TaskLock`, `LeaderElection`, `RateLimitFilter`, `DuplicateFilter`, `HealthRegistry`, `log.configure`) follows the same shape:

| Surface | Form | Intent |
|---|---|---|
| `__init__(name, **kwargs)` | Positional name + optional fields | Programmatic and environmental construction |
| `from_config(name, config)` | Positional name + frozen config | Declarative construction from a settings tree |

Variant-driven components (`RateLimiter`) substitute the `__init__` surface with factory classmethods (`RateLimiter.token_bucket`, `RateLimiter.gcra`) but keep `from_config` unchanged.

The `Config` Pydantic class carries settings only. Identity (the instance name) lives on the component, never inside the config object. This matches the `Map<name, Settings>` shape that YAML and `pydantic-settings` aggregations produce naturally.

## `resolve_config()`

All merging happens once in `grelmicro._config.resolve_config()`:

```python
config = resolve_config(
    LockConfig,
    explicit=None,
    kwargs={"lease_duration": lease_duration, ...},
    env_prefix=env_prefix or f"GREL_LOCK_{env_segment(name)}_",
    read_env=read_env,
)
```

The function returns a frozen `LockConfig`. From that point on, the component reads fields off `self._config` directly. There is no per-call merging, no env lookup, and no validation on the hot path.

## Name normalisation

Instance names are normalised before they enter an env prefix so that natural identifiers produce valid POSIX environment variables. The rule, implemented as `grelmicro._config.env_segment`:

1. Upper-case the name.
2. Replace any character outside `[A-Z0-9_]` with `_`.
3. Collapse runs of underscores into one.
4. Strip leading and trailing underscores.

A name that produces an empty segment or one starting with a digit is rejected at construction with an actionable error.

## Hot-path discipline

The config model is a frozen `BaseModel` with `extra="forbid"`. Field reads cost ~2 ns and account for ~1% of a 255 ns `RateLimitFilter.filter` call. This is the budget the design protects:

- Validation runs once at construction, never on a request.
- Env reads happen at construction, never on a request.
- The hot path holds a reference to one Pydantic instance and reads attributes.

This is why the resolution table (kwargs > env > default) is materialised into `self._config` and never re-evaluated. Runtime reconfiguration, when added, will atomically swap the `self._config` pointer without touching the resolution machinery.

## Why `from_config` skips the env layer

`from_config(name, cfg)` is the declarative path. The caller has already merged whatever sources they want (YAML, Vault, `pydantic-settings`). Re-reading env on top would silently invert the priority and make composition non-deterministic. The contract is: what you pass is what runs.

## Where `Config` classes live

| Class | Module |
|---|---|
| `LockConfig` | `grelmicro.sync.lock` |
| `TaskLockConfig` | `grelmicro.sync.tasklock` |
| `LeaderElectionConfig` | `grelmicro.sync.leaderelection` |
| `CircuitBreakerConfig` | `grelmicro.resilience.circuitbreaker` |
| `RateLimiterConfig` (discriminated union) | `grelmicro.resilience.algorithms` |
| `RateLimitFilterConfig` | `grelmicro.log` |
| `DuplicateFilterConfig` | `grelmicro.log` |
| `HealthRegistryConfig` | `grelmicro.health` |
| `LoggingConfig` | `grelmicro.log` |

Each is a `BaseModel, frozen=True, extra="forbid"`. Field docs live in `Annotated[T, Doc("...")]` blocks and surface in IDEs and the API reference.

## Related

- [Configuration](../config.md) — the user-facing guide for the three paths, prefix table, and recipes.
- [Backend Registry](backends.md) — companion contract for runtime-pluggable backends.
