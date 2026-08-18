# Configuration internals

This page is the engineering side of [Configuration](../config.md). It assumes you already know the three paths and the resolution order. It documents the machinery that makes them deterministic and cheap.

## The contract

Components fall in two categories.

**Multi-instance components** (`Lock`, `TaskLock`, `LeaderElection`, `CircuitBreaker`) take a positional `name` because an application typically holds several of each (`Lock("cart")`, `Lock("checkout")`):

| Surface | Form | Intent |
|---|---|---|
| `__init__(name, **kwargs)` | Positional name + optional fields | Programmatic and environmental construction |
| `from_config(name, config)` | Positional name + frozen config | Declarative construction from a settings tree |

**Single-instance components** (`HealthChecks`, `Log`, `Trace`, `Metrics`, `RateLimitFilter`, `DuplicateFilter`, `log.configure`) drop the positional name because the application typically holds one:

| Surface | Form | Intent |
|---|---|---|
| `__init__(**kwargs)` | Optional fields only | Programmatic and environmental construction |
| `from_config(config)` | Frozen config only | Declarative construction from a settings tree |

**Variant-driven components** (`RateLimiter`) substitute the `__init__` surface with factory classmethods (`RateLimiter.token_bucket(name, ...)`, `RateLimiter.sliding_window(name, ...)`) but keep `from_config(name, config)` unchanged.

The `Config` Pydantic class carries settings only. For multi-instance components the identity lives on the component, never inside the config object. This matches the `Map<name, Settings>` shape that YAML and `pydantic-settings` aggregations produce naturally.

## The invariant

One sentence governs every environment variable grelmicro reads:

!!! quote ""
    Every value field of a named grelmicro object resolves once at
    construction, in the fixed order keyword argument, `GREL_{KIND}_{NAME}_{FIELD}`,
    `GREL_{KIND}_{FIELD}`, app-wide variable, default, gated by `GREL_ENV_LOAD`.
    The environment tunes any value. It never chooses a name, an algorithm, or a
    backend, and it never fails silently.

The rules below all follow from it. If a rule and the invariant disagree, the
invariant wins and the rule is a bug.

### The rules

**R1 Namespace.** grelmicro tuning lives under `GREL_*`. A Provider reads its
vendor's own namespace instead (`REDIS_*`, `VALKEY_*`, `POSTGRES_*`,
`SQLITE_*`), ungated, because those names belong to the deployment rather than
to grelmicro. A missing required connection value fails at construction.

**R2 Gate.** Every `GREL_*` field read is gated by `GREL_ENV_LOAD`. A variable
that fills no field, such as `GREL_ENV_LOAD` itself, is read ungated.

**R3 Address.** The environment addresses identity: `GREL_{KIND}_{NAME}_{FIELD}`,
with the name segment dropped for the `default` instance. No name means no
address, which is why `TTLCache` reads nothing and has no live reload.

**R4 Segment.** A component's segment is its kind string uppercased (`health`
gives `HEALTH`). A pattern's segment is its class name uppercased, separators
dropped, singular (`Tasks` gives `TASK`, `LeaderElection` gives
`LEADERELECTION`). Every shipped prefix derives from this, so a prefix never
needs renaming again.

**R5 Merge.** Per field, in the invariant's order. A caller who passes some
fields and not others gets the rest filled independently. Resolution happens
once at construction, never on the hot path.

**R6 Structure is code.** The environment fills values. Names, algorithm `kind`,
backends, providers, serializers, and callables come from code. A `kind`
variable that contradicts the code fails validation at startup.

*Presets are values.* A variable may choose between config classes only when
every class declares the identical field names and drives the same runtime
code. Such a choice retunes constants and can never make a set variable start
or stop applying. `GREL_SHIELD_{NAME}_PROFILE` qualifies: the three profiles
share every field and feed one state machine, differing only in frozen
constants. The moment two classes differ by a single field, the choice selects
an algorithm and belongs to code. A test pins this, so preset status is earned
by the field sets, never claimed.

**R7 Never silently dropped.** A variable naming a field the pattern declares,
in any of its algorithms, is always accounted for:

| Situation | What happens |
|---|---|
| Gate off, variable set | `EnvLoadOffWarning` naming the variable |
| Instance address, field of another algorithm | Error at construction, naming the algorithm that is running |
| Kind address, field of another algorithm | Applied where it fits, ignored where it does not, silently, because the kind address is a broadcast |
| Live reload, field of another algorithm | Skipped for that instance and counted at debug, never a crash. Key names stay out of the logs because a mounted Secret's key can itself be sensitive. A warning naming sibling fields follows with config provenance ([#664](https://github.com/grelinfo/grelmicro/issues/664)). |
| Value invalid | `SettingsValidationError` naming the variable and the reason, never the value |

Every class raises that one error, whichever pattern or component it is. There
is no per-module subclass to remember: a bad value is a startup crash, and the
message already names the exact variable, which locates the problem better than
a class name can. A config class you build yourself, such as `RetryConfig(...)`
or `ExponentialBackoff(...)`, raises pydantic's `ValidationError` like any
pydantic model.

For the default instance the instance address is the kind address, so it
follows the broadcast row: the bare prefix cannot tell "this instance's field"
from "every instance's field", and an ambiguous address is not an unambiguous
mistake.

A rejected *value* is never echoed, with no exceptions. A rejected *name* is, and the next rule says why. A variable name is the
operator's own, but its value can be a credential, so an error carries the name
and the reason and stops there.

There is no closed-set exemption. It is tempting to argue that a field whose
domain is public, such as a timezone name, can safely repeat what it rejected.
The argument inverts on contact: the domain describes the values that are
*accepted*, while the echoed string is by definition one that was not, which is
arbitrary operator input. `GREL_TIMEZONE` is an app-wide variable every `Log`
and `Tasks` reads, so a swapped variable puts a credential there.

Name a *member* of the set instead. `did you mean 'Europe/Zurich'` diagnoses a
typo better than repeating it does, and a member name can never be a secret.

Three mechanisms keep this true, because one is not enough:

1. Wrapping into `SettingsValidationError` drops the input pydantic attaches to
   every error it raises, for every field, reviewed or not.
2. grelmicro's own validators are written not to name the value.
3. `SettingsValidationError` scrubs the input out of the message as a backstop,
   because some messages are built by pydantic (`union_tag_invalid` quotes the
   tag, `import_error` quotes the module) or by a config class it does not own.
   Whole occurrences are removed, never fragments: a typo is usually a prefix
   of the name that was meant, so removing pieces took the correct spelling out
   of the very message offering it.

A name is code, not a value. R3 makes an instance name the address the
environment writes to, and R6 keeps structure in code, so a rejected name is
repeated in the error: it is a literal the caller wrote, not something read
from a variable, and the message is useless without it. The error is still
`SettingsValidationError`, because a caller should not have to know whether
their bad input counted as configuration or as identity.

A validator raises `ValueError`, never `TypeError`. Pydantic converts only
`ValueError` and `AssertionError` into a validation error, so a `TypeError`
escapes `except SettingsValidationError` and `except ValueError` alike, and
escapes the live-reload loop too.

A name matching no declared field of any algorithm is ignored without report.
Kubernetes injects `{SVCNAME}_SERVICE_HOST` into every pod, so grelmicro must
not warn on names it does not own.

**R8 One declarative door.** `from_config` takes the config as-is: no variable
is read and the instance is not registered for live reload. Every other
construction door resolves the environment and registers.

**R9 Sources do not self-configure.** A component that feeds the configuration
layer cannot read from it, so `ExternalConfig` is configured in code only.

### What enforces this

Each rule above is swept over the whole family it applies to, not checked on
one member. `tests/test_config_contracts.py` covers R3, R4 and the reload
rules, `tests/test_construction_contracts.py` covers R8 and the one
declarative door, and `tests/test_settings_error_contract.py` covers the
error a bad value raises and the promise that it never carries the value.

Every sweep refuses to pass on an empty scan. The rules here were each
written down after being broken once, and in every case the rule held for a
family while only one member was tested.

### Settled

Decisions that are closed, with the assumption each rests on. When an
assumption stops holding, the decision is worth reopening. Until then it is
not.

| Decision | Assumption it rests on | Reopen when |
|---|---|---|
| `TTLCache` reads no environment | The environment addresses identity, and a nameless object has no address (R3) | `TTLCache` gains a name |
| The environment tunes an algorithm's fields, never selects the algorithm | Code owns structure, the environment owns values (R6) | A config becomes genuinely selectable from outside code |
| The merge is per field, not all-or-nothing | A mounted file already patches per key at runtime, so an all-or-nothing rule at construction would delay the surprise rather than remove it | Live reload stops patching per key |
| The kind address is a broadcast and stays silent | A fleet legitimately runs both algorithms and tunes one of them kind-wide | Kind-wide tuning stops being a real deployment shape |
| `from_config` is the one door for a pre-built config | The environment-merging lane and the config-is-truth lane must be distinguishable at the call site | The environment lane is removed |
| `GREL_SHIELD_PROFILE` selects a preset, not an algorithm | Every profile declares the identical field names, so no variable gains or loses meaning from the choice | A profile adds or removes a field |
| A Provider reads its vendor namespace, not `GREL_*` | Connection settings belong to the deployment, and every vendor already defines those names | grelmicro starts owning connection settings |
| One `SettingsValidationError` for every class, no per-module subclass | No caller reacts differently to a bad value by module, and the message names the variable | A caller needs to branch on the module a config error came from |
| The rejected value is never echoed, with no closed-set exemption | The echoed string is one the domain rejected, so it is arbitrary input whatever the field accepts | A field's input is bounded before it reaches the message |
| A rejected name is echoed, a rejected value is not | R3 makes a name the address the environment writes to and R6 keeps structure in code, so a name is a literal the caller wrote | Names start arriving from the environment |
| A validator raises `ValueError`, never `TypeError` | Pydantic converts only `ValueError` and `AssertionError`, so a `TypeError` escapes every documented `except` | Pydantic converts `TypeError` too |

## `resolve_config()`

All merging happens once in `grelmicro._config.resolve_config()`:

```python
config = resolve_config(
    LockConfig,
    explicit=None,
    kwargs={"lease_duration": lease_duration, ...},
    env_prefix=env_prefix or default_env_prefix("LOCK", name),
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

## The bare prefix is the kind default

`grelmicro._config.default_env_prefix` builds the prefix from the component and the name. The default instance drops the name segment, so a `Lock("default")` reads `GREL_LOCK_LEASE_DURATION`, not `GREL_LOCK_DEFAULT_LEASE_DURATION`. A named instance keeps it: `Lock("cart")` reads `GREL_LOCK_CART_LEASE_DURATION`.

| Instance | Prefix | Falls back to |
|---|---|---|
| `Lock("default")` | `GREL_LOCK_` | nothing, it is already the bare prefix |
| `Lock("cart")` | `GREL_LOCK_CART_` | `GREL_LOCK_` |

The bare `GREL_{COMPONENT}_` namespace is the **kind default**. Every instance falls back to it, so one variable retunes a whole kind:

```bash
GREL_LOCK_LEASE_DURATION=60   # every Lock, named or not
```

A named instance still wins for itself:

```bash
GREL_LOCK_LEASE_DURATION=60        # every Lock
GREL_LOCK_CHECKOUT_LEASE_DURATION=300   # except this one
```

Build both prefixes with `grelmicro._config.env_prefixes`, which returns the instance prefix and the kind prefix to fall back to. It returns `None` for the kind prefix when there is nothing to fall back to: the default instance already owns the bare prefix, and a caller-supplied `env_prefix=` means "read exactly these variables", so grelmicro does not add its own namespace underneath it.

The trade-off: a named instance whose name collides with a field prefix can alias a kind field. A `Lock("lease")` reads `GREL_LOCK_LEASE_DURATION` for a field named `duration`, the same key the kind default uses for `lease_duration`. Under the kind-default rule this reaches **every** lock rather than only the default one, so the blast radius is wider than it looks. The rule: name instances so their segment cannot start a field name of the same component.

## App-wide variables

Almost every grelmicro variable belongs to one component instance and is named
`GREL_{COMPONENT}_{FIELD}`. A few belong to the process instead. Those drop the
component segment and read `GREL_{NAME}`.

| Variable | Meaning |
|---|---|
| `GREL_ENV_LOAD` | Turns the environment path on for every component. |
| `GREL_TIMEZONE` | The wall clock the service's business rules run on. |

A variable qualifies as app-wide only when it meets all four rules.

1. **More than one component reads it.** A value only `Log` uses stays
   `GREL_LOG_*`. `GREL_TIMEZONE` is read by `Tasks` and by `Log`.
2. **Every reader agrees on what it means.** One sentence has to describe the
   value for all of them. Two components that would want different values in a
   normal deployment need separate fields, not a shared one.
3. **A component variable overrides it.** The app-wide value is the fallback,
   never the winner. `GREL_LOG_TIMEZONE` beats `GREL_TIMEZONE`, and a keyword
   argument beats both.
4. **It is read once at startup.** App-wide values are not reconfigurable.
   `ExternalConfig` matches keys by component prefix, so a bare `GREL_*` key in
   a mounted ConfigMap matches nothing and is ignored. A component that needs a
   live value keeps its own field.

The precedence slot sits between the kind default and the field default, so the
full order for a named instance reads:

```
keyword argument
  > GREL_{COMPONENT}_{NAME}_{FIELD}   the instance
  > GREL_{COMPONENT}_{FIELD}          the kind default
  > GREL_{NAME}                       the app-wide value
  > field default
```

`GREL_ENV_LOAD` gates the app-wide layer too. A `GREL_TIMEZONE` set without it
is ignored and reported like any other variable.

The mechanism is `shared_env` on `resolve_config`. It passes the shared variable
name to `_build_settings_cls`, which redeclares the field on the dynamic
subclass with `validation_alias=AliasChoices(f"{env_prefix}{FIELD}",
shared_var)`. The prefix is composed from the resolved prefix, so a custom
`env_prefix=` still works. All matching, including case, is left to
pydantic-settings. The subclass sets `populate_by_name` so the field name still
works as a keyword argument under `extra="forbid"`, and the field is copied
rather than rebuilt so its metadata and default factory survive.

Adding a new app-wide variable is a design decision, not a convenience. Two are
enough for most services, and each one costs every reader a lookup slot and
every operator a name to learn. A value that describes deployment rather than
behaviour, such as a region or a cluster name, belongs in your own settings
object and reaches grelmicro as a keyword argument. Open an issue before adding
a third.

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
| `LogConfig` | `grelmicro.log` |
| `TasksConfig` | `grelmicro.task` |

Each is a `BaseModel, frozen=True, extra="forbid"`. Field docs live in `Annotated[T, Doc("...")]` blocks and surface in IDEs and the API reference.

## Related

- [Configuration](../config.md): the user-facing guide for the three paths, prefix table, and recipes.
- [Backends and Adapters](backends.md): companion contract for runtime-pluggable backends.
