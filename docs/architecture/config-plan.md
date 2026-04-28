# Configuration rollout plan

Sibling document to [Configuration](config.md). That document defines the contract. This one defines how it ships.

## Principles

- **Docs first.** No production code lands before the architecture and user-facing docs are approved. The Lock component is the pilot and the only one with docs in this pass.
- **One component at a time.** After Lock lands and bakes, the other components follow with mechanical copies of the same pattern. Each gets its own PR.
- **Additive, never breaking.** Every existing call site continues to work unchanged. The `config=`, `env_prefix=`, `read_env=` kwargs are all new and optional. Existing kwargs keep their current defaults.
- **Benchmark before and after.** The `benchmarks/config_attr_benchmark.py` script stays in the repo. Any perf-sensitive change requires a before/after run with the numbers linked in the PR.

## Scope in this pass

Docs only, on branch `docs/config-architecture`:

1. `docs/architecture/config.md` — this architecture doc.
2. `docs/architecture/config-plan.md` — this plan.
3. `docs/sync.md` — pilot user-facing docs for `Lock` showing the three paths.
4. `docs/snippets/sync/` — new example files demonstrating programmatic, declarative, and environmental paths.

No Python changes. No test changes. No changelog entry yet. The plan reaches stable shape first.

## Rollout order once docs are approved

Each step is its own PR, merged before the next starts.

### Step 1 — shared helper
Introduce `grelmicro/_config.py::resolve_config`. Pure function, no component code depends on it yet. Unit-tested in isolation. Roughly 25 lines.

Acceptance:

- All four paths (programmatic, declarative, environmental, error on mixing) have tests.
- Auto-derived prefix tests cover multi-instance and single-instance shapes.
- `env_prefix` override and `read_env=False` are covered.
- `resolve_config` is private (`_config.py`), not exported.

### Step 2 — Lock pilot
Drop `name` from `BaseLockConfig` (Config carries settings only). Wire `Lock.__init__` to `resolve_config` for the kwargs-and-env path. Add `env_prefix` and `read_env` kwargs. Add `Lock.from_config(name, config, *, backend=None)` classmethod for the declarative path. Add `Lock.name` property. Existing positional `Lock(name, ...)` call sites unchanged.

Acceptance:

- All three call shapes from the user-facing doc work end-to-end.
- `Lock("cart")` with `GREL_LOCK_CART_LEASE_DURATION=60` in env yields `config.lease_duration == 60`.
- `Lock.from_config("cart", cfg)` ignores matching `GREL_LOCK_*` env vars.
- `lock.name` returns the positional name. `lock.config` returns settings only.
- Benchmark re-run shows no regression in `RateLimitFilter` path (unrelated, but sanity check).
- Changelog entry added.

### Step 3 — other multi-instance components
Mechanical application of the Lock pattern:

- `TaskLock` — prefix `GREL_TASK_LOCK_{NAME_UPPER}_`.
- `RateLimiter` — prefix `GREL_RATE_LIMITER_{NAME_UPPER}_`.
- `LeaderElection` — prefix `GREL_LEADER_ELECTION_{NAME_UPPER}_`.

Each ships in its own PR with the same acceptance checks as Lock.

### Step 4 — single-instance components
- `RateLimitFilter` — no name in constructor today, so either treat `capacity`/`key_mode` as the config and use prefix `GREL_RATE_LIMIT_FILTER_`, or introduce an optional name for parity with the other filters. Pick during the PR.
- `DuplicateFilter` — same pattern as `RateLimitFilter`. Prefix `GREL_DUPLICATE_FILTER_`.
- `HealthRegistry` — prefix `GREL_HEALTH_`.

### Step 5 — logging realignment
Split `LoggingSettings` into `LoggingConfig(BaseModel)` (canonical, lowercase fields, no env) and keep `LoggingSettings(LoggingConfig, BaseSettings)` as the opt-in convenience with `env_prefix="GREL_LOG_"`. Field names move from `LOG_BACKEND`/`LOG_LEVEL` uppercase to lowercase `backend`/`level`. Env vars move from `LOG_*` to `GREL_LOG_*` to align with the rest of the library.

Migration is the only breaking change in this initiative. It is split across two releases.

Release N (the step 5 release):

- Read both `GREL_LOG_*` and `LOG_*` environment variables. When only `LOG_*` is set, emit a `DeprecationWarning` that names each variable.
- `LoggingSettings` keeps accepting `LOG_BACKEND=...` style kwargs through a compat alias, also with `DeprecationWarning`.
- `LoggingConfig` is the new canonical import for apps that want their own env prefix.
- Changelog entry flags the deprecation and points at the new env var names.

Release N+1:

- `LOG_*` env support removed. Only `GREL_LOG_*` is read.
- Uppercase kwarg aliases removed.

Acceptance for release N:

- `GREL_LOG_LEVEL=DEBUG` works end-to-end.
- `LOG_LEVEL=DEBUG` still works with one `DeprecationWarning` per variable.
- `LoggingSettings()` instance shape matches `LoggingConfig` (lowercase attrs).
- `configure_logging()` signature keeps its current shape.

### Step 6 — unified user docs
Top-level `docs/configuration.md` page indexing the three paths with one example per component. Link from every component page. Snippets live in `docs/snippets/configuration/`.

This step also revisits the YAML-loading ergonomics. The current pilot snippet shows env-only composition with `pydantic-settings`. YAML composition through `settings_customise_sources` + `YamlConfigSettingsSource` is verbose enough that a grelmicro convenience (for example a `from_yaml(path)` helper or a documented one-liner using `yaml.safe_load` + `LockConfig`) is worth shipping at this stage. Pick the lighter option during the PR.

### Step 7 — close issue #113
With the architecture doc committed, close #113 with a reference to the decision rationale section. Include a link to `benchmarks/config_attr_benchmark.py` and the measured numbers.

## Out of scope for this initiative

- `reconfigure()` method — design stays compatible, implementation deferred until a concrete use case (ConfigMap reload, file watcher, refresh endpoint) lands.
- Rust / PyO3 hot-path ports.
- File watcher or signal-based reloaders.
- Registry lookup for `backend="redis"` string alias.

## Risks and mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Dynamic `BaseSettings` subclass inside `resolve_config` surprises type checkers or breaks downstream introspection. | Low | Keep the dynamic class local to one call. Never expose the type. Add a test that round-trips a resolved config via `model_dump`/`model_validate` if it matters downstream. |
| Auto-derived env prefix collides with unrelated env vars (e.g. `GREL_LOCK_CART_FOO` already set by the platform). | Low | The `GREL_` namespace minimises this. `read_env=False` and `env_prefix=` escape hatches cover the remainder. Document the derivation rule prominently. |
| Apps currently using `LoggingSettings` rely on attribute names like `.LOG_LEVEL`. | Low | Keep compat aliases for one release. Field names on the new `LoggingConfig` use clean names (`level`, `format`). The BaseSettings mixin translates env to them via `env_prefix`. |
| Step 1 ships a helper that no component uses yet. | None | Acceptable. The helper is unit-tested on its own. Step 2 wires it in. |
| Scope creep into `reconfigure()` or Rust. | Medium | This plan explicitly rejects both. Link back here if it comes up. |
