# Configuration internals

This page is the engineering side of [Configuration](../config.md). It assumes you already know the three paths and the resolution order. It documents the machinery that makes them deterministic and cheap.

## The contract

Components fall in two categories.

**Multi-instance components** (`Lock`, `TaskLock`, `LeaderElection`, `CircuitBreaker`) take a positional `name` because an application typically holds several of each (`Lock("cart")`, `Lock("checkout")`):

| Surface | Form | Intent |
|---|---|---|
| `__init__(name, **kwargs)` | Positional name + optional fields | Programmatic and environmental construction |
| `from_config(name, config)` | Positional name + frozen config | Declarative construction from a settings tree |

**Single-instance components** (`HealthChecks`, `RateLimitFilter`, `DuplicateFilter`, `log.configure`) drop the positional name because the application typically holds one:

| Surface | Form | Intent |
|---|---|---|
| `__init__(**kwargs)` | Optional fields only | Programmatic and environmental construction |
| `from_config(config)` | Frozen config only | Declarative construction from a settings tree |

**Variant-driven components** (`RateLimiter`) substitute the `__init__` surface with factory classmethods (`RateLimiter.token_bucket(name, ...)`, `RateLimiter.sliding_window(name, ...)`) but keep `from_config(name, config)` unchanged.

The `Config` Pydantic class carries settings only. For multi-instance components the identity lives on the component, never inside the config object. This matches the `Map<name, Settings>` shape that YAML and `pydantic-settings` aggregations produce naturally.

## `resolve_config()`

All merging happens once in `grelmicro._config.resolve_config()`:

```python
config = resolve_config(
    LockConfig,
    explicit=None,
    kwargs={"lease_duration": lease_duration, ...},
    env_prefix=env_prefix or f"GREL_LOCK_{env_segment(name)}_",
    env_load=env_load,
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

The config model is a frozen `BaseModel` with `extra="forbid"`. The hot path holds one reference to that instance and reads attributes off it. This is the budget the design protects:

- Validation runs once at construction, never on a request.
- Env reads happen at construction, never on a request.
- Resolution (kwargs > env > default) materialises into `self._config` and is never re-evaluated.

Runtime reconfiguration, when added, will atomically swap the `self._config` pointer without touching the resolution machinery.

### Why we don't copy fields to plain instance attrs

Pydantic model attribute access is measurably slower than a plain instance attribute lookup, because field reads go through Pydantic's customized `__getattribute__` and `__pydantic_fields__` machinery. The shortcut is to mirror every frozen field onto the component (`self._name = self._config.name`, ...) so hot paths read `self._name`. We don't take it, on purpose.

Typical numbers from `benchmarks/config_attr_benchmark.py` (Issue [#113](https://github.com/grelinfo/grelmicro/issues/113)) on a developer laptop:

| Read pattern | ns / field | Ratio |
|---|---:|---:|
| Pydantic attr (`self._config.x`) | ~11 | 1.5× |
| Plain attr (`self._x`) | ~7 | 1.0× |
| Frozen slotted dataclass | ~8 | 1.2× |

Realistic hot path: `RateLimitFilter.filter` per log record (~250 ns total):

| | ns / call | Share |
|---|---:|---:|
| Total call | ~250 | 100% |
| Minus the one config read | ~248 | ~99% |
| **Config read cost** | **~2** | **<1%** |

A ~2 ns saving per call disappears in the surrounding dict, lock, and math work. Against that:

- **Duplicated state.** Each mirrored field stores its value twice (once on the frozen `BaseModel`, once in the component's `__dict__`). Trivial in absolute bytes, but two sources of truth where one would do.
- **Code surface.** 43 hot-path reads across 5 modules become 43 mirror copies plus a duplication rule every new field has to follow.
- **Desync risk.** A contributor who updates the Pydantic field and forgets the mirror introduces silent drift between `cb.config.x` and the cached `cb._x`.

We keep `self._config` as the single source of truth. If a future profile shows config attribute access on the critical path of a tight loop where it actually matters, revisit per-call site, not as a sweeping refactor.

## Why `from_config` skips the env layer

`from_config(name, cfg)` is the declarative path. The caller has already merged whatever sources they want (YAML, Vault, `pydantic-settings`). Re-reading env on top would silently invert the priority and make composition non-deterministic. The contract is: what you pass is what runs.

## Where `Config` classes live

| Class | Module |
|---|---|
| `LockConfig` | `grelmicro.coordination.lock` |
| `TaskLockConfig` | `grelmicro.coordination.tasklock` |
| `LeaderElectionConfig` | `grelmicro.coordination.leaderelection` |
| `CircuitBreakerConfig` | `grelmicro.resilience.circuitbreaker` |
| `RateLimiterConfig` (discriminated union) | `grelmicro.resilience.ratelimiter` |
| `RateLimitFilterConfig` | `grelmicro.log` |
| `DuplicateFilterConfig` | `grelmicro.log` |
| `HealthChecksConfig` | `grelmicro.health` |
| `LoggingConfig` | `grelmicro.log` |

Each is a `BaseModel, frozen=True, extra="forbid"`. Field docs live in `Annotated[T, Doc("...")]` blocks and surface in IDEs and the API reference.

## Related

- [Configuration](../config.md): the user-facing guide for the three paths, prefix table, and recipes.
- [Backend Registry](backends.md): companion contract for runtime-pluggable backends.
