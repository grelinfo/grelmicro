# Changelog

## Unreleased

### Breaking

* 💥 Backend constructors are now pure: `__init__` performs no registry writes. The `auto_register` kwarg is removed from every backend and from `HealthRegistry`. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* 💥 `BackendRegistry.set` is renamed `register` and `BackendRegistry.unregister` is added with an identity check. `reset` remains for test fixtures. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* 💥 `async with backend` opens the connection but no longer registers. Call `register(backend)` (or `use_backend(backend)`) to register, or open everything at once with `grelmicro.lifespan()`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 `BackendRegistry` is now multi-name: `register(backend, name="default")`, `unregister(name, backend=None)`, `get(name="default")`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 The sync registry name changed from `"lock"` to `"sync"` (used in error messages and `lifespan()` exclude keys); the rate limiter registry from `"rate_limiter"` to `"resilience"`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 Overwriting a registered name with a different instance now raises `BackendAlreadyRegisteredError` (was: warning + replace). Re-registering the same instance stays a no-op. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).

### Features

* ✨ Add `grelmicro.sync.use_backend`, `grelmicro.cache.use_backend`, `grelmicro.resilience.use_backend`, and `grelmicro.health.use_registry` for explicit, idempotent process-lifetime registration. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* ✨ `grelmicro.lifespan(*ad_hoc, exclude=...)` opens every registered backend across every imported module in one call, with reverse-order shutdown. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Per-module helpers `register`, `unregister`, `use_backend`, `use` on `grelmicro.sync`, `grelmicro.cache`, `grelmicro.resilience` (and `use_registry`, `use` on `grelmicro.health`). PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Task-scoped overrides via `<module>.use(backend)` or `<module>.use(default=a, analytics=b)`. Stacks LIFO via `contextvars` for per-test and per-request substitution. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Primitives accept `backend=` as either a backend instance or a registered name (`Lock("audit", backend="analytics")`). The registry is consulted on each call so `<module>.use(...)` overrides apply. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Registries subscribe themselves on import: `lifespan()` walks only modules that are actually imported, so unused components have zero RAM cost and zero startup work. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Lookup falls back to the sole registered entry when no `"default"` is named, so the single-backend case stays one-call. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).

## 0.17.0 - 2026-04-29

### Breaking

* 💥 `CircuitBreaker` config moves to a frozen `CircuitBreakerConfig`. Read it via `cb.config`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* 💥 The mutable attributes `cb.error_threshold`, `cb.success_threshold`, `cb.reset_timeout`, `cb.half_open_capacity`, `cb.ignore_exceptions`, `cb.log_level` are removed. Construct a new `CircuitBreaker` to change config. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* 💥 Rename `grelmicro.logging` to `grelmicro.log` and `grelmicro.tracing` to `grelmicro.trace`. Avoids shadowing stdlib `logging` and aligns with the OpenTelemetry / `ddtrace` `trace` (singular) convention. Update imports: `from grelmicro import log, trace`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `configure_logging()` is renamed `log.configure()`. Use `log.configure_with(config)` for the declarative path. Both return the applied `LoggingConfig`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingSettings` (the `BaseSettings` shadow class) is removed. `LoggingConfig` is the canonical config class. Env reading happens inside `log.configure()`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingConfig` field names move to lowercase: `LOG_BACKEND` → `backend`, `LOG_LEVEL` → `level`, `LOG_FORMAT` → `format`, `LOG_TIMEZONE` → `timezone`, `LOG_JSON_SERIALIZER` → `json_serializer`, `LOG_CALLER_ENABLED` → `caller_enabled`, `LOG_OTEL_ENABLED` → `otel_enabled`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 Env vars move from `LOG_*` to `GREL_LOG_*` to align with the rest of the library. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingSettingsValidationError` is removed. `pydantic.ValidationError` propagates from `log.configure()` like every other component. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).

### Features

* ✨ Add `CircuitBreakerConfig` and `CircuitBreaker.from_config(name, config)`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `CircuitBreaker` reads `GREL_CIRCUIT_BREAKER_<NAME>_*` env vars and accepts `env_prefix=` / `read_env=`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `ignore_exceptions` accepts fully-qualified import strings (`"builtins.ValueError"`) so YAML and env loaders can specify exception types. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ Env vars for tuple/list fields accept comma-separated values in addition to JSON arrays. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `log.configure(**kwargs)` accepts every `LoggingConfig` field as a kwarg, mirroring the three-paths contract used by other components. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* ✨ `log.configure_with(config)` is the declarative entry point. Returns the applied `LoggingConfig`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).

### Internal

* ♻️ Add `grelmicro/_types.py` for shared lightweight type aliases (`LogLevel`). PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ♻️ Add `grelmicro/_config.py::parse_csv_or_json` shared utility for env var list parsing. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).

### Docs

* 🎨 Switch logo typeface from Funnel Sans to Funnel Display.

## 0.16.1 - 2026-04-29

### Internal

* ✅ "No registry call at construction" tests now patch the registry source instead of the per-module import alias, so a future refactor that bypasses the local alias can no longer silently pass the check. PR [#130](https://github.com/grelinfo/grelmicro/pull/130).
* ⬆️ Bump `ty` from 0.0.29 to 0.0.30. PR [#111](https://github.com/grelinfo/grelmicro/pull/111).
* ⬆️ Pre-commit autoupdate. PR [#114](https://github.com/grelinfo/grelmicro/pull/114).

## 0.16.0 - 2026-04-29

### Breaking

* 💥 `LockConfig`, `TaskLockConfig`, `LeaderElectionConfig`, and `RateLimiterConfig` no longer carry a `name` field. Pass the name positionally: `Lock("cart", LockConfig(lease_duration=30))`. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 Rename `TokenBucket` to `TokenBucketConfig` and `GCRA` to `GCRAConfig`. `RateLimiterConfig` becomes the discriminated union of algorithm configs. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 `RateLimiter` takes the algorithm config positionally: `RateLimiter("api", GCRAConfig(limit=100, window=60))`. The `algorithm=`, `limit=`, `window=` kwargs are removed. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 `fail_open` moves from `RateLimiter(...)` to the algorithm config. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).

### Features

* ✨ Add `Component.from_config(name, config)` to every primitive (`Lock`, `TaskLock`, `LeaderElection`, `RateLimiter`, `HealthRegistry`, `RateLimitFilter`, `DuplicateFilter`). PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Read environment variables under `GREL_<COMPONENT>_<NAME>_*` for every component that supports the environmental path. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Add `RateLimiter.token_bucket(name, ...)` and `RateLimiter.gcra(name, ...)` factory classmethods. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Add `env_prefix=` and `read_env=` kwargs to every component that exposes the environmental path. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Normalise instance names like `payments-eu`, `cart.v2`, or `weather/svc` into POSIX env var segments. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).

### Changed

* ♻️ `Lock`, `TaskLock`, `LeaderElection`, and `RateLimiter` now resolve the backend lazily on first use instead of at construction. `BackendNotLoadedError` surfaces on the first `acquire`/`peek`/`reset` call rather than in `__init__`. Each component exposes a public `backend` property. PR [#128](https://github.com/grelinfo/grelmicro/pull/128).

### Fixed

* 🐛 Auto-registered backends now identity-check before clearing the registry on `__aexit__`, so a replacement instance is left alone. PR [#122](https://github.com/grelinfo/grelmicro/pull/122).
* 🐛 `Lock.release` clears local ownership only after the backend confirms the release. PR [#122](https://github.com/grelinfo/grelmicro/pull/122).

## 0.15.0 - 2026-04-29

### Breaking

* 💥 Redesign the `health` module: `@health.check("name")` decorator, binary `ok`/`error` status, empty probe bodies, per-check caching. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 Endpoint renames: `/health/live` → `/livez`, `/health/ready` → `/readyz`. New `/healthz` returns the full check JSON. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthRegistry.check()` renamed to `run()`. The `check` name is now the decorator. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthChecker` Protocol removed. Use plain `def` or `async def` functions. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthReport.components: list` becomes `HealthReport.checks: dict[name, ...]`. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthCheckTimeoutError` and the three-state `HealthStatus` removed. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).

### Docs

* 📝 Restate the versioning policy: pre-1.0 `MINOR` may break, `PATCH` never. Post-1.0 deprecations get two `MINOR` releases.

## 0.14.3 - 2026-04-22

### Docs

* 🐛 Fix the wordmark duplicating on PyPI and other renderers that don't understand GitHub's theme-only URL fragments. PR [#109](https://github.com/grelinfo/grelmicro/pull/109).

## 0.14.2 - 2026-04-22

### Docs

* 🐛 Fix the landing-page wordmark disappearing when the docs site is toggled into dark mode. PR [#108](https://github.com/grelinfo/grelmicro/pull/108).
* 📝 Centre the badges row under the tagline. PR [#108](https://github.com/grelinfo/grelmicro/pull/108).

## 0.14.1 - 2026-04-22

### Docs

* 🎨 Ship the grelmicro brand identity: wordmark, favicon, and social-preview card. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 🎨 Refresh the docs theme with the brand palette. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 📝 Rewrite the "Why grelmicro" pillars. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 📝 Split the resilience docs into per-pattern pages.
* 📝 Add an Installation guide with `pip`, `uv`, and `poetry` tabs.
* 📝 Render PEP 727 `Annotated[..., Doc(...)]` parameter docs via `griffe-typingdoc`.
* 📝 Plain-English pass on docs and docstrings for non-native readers.
* 📝 Add a Mermaid state diagram to the Circuit Breaker page.
* 📝 Document every `__all__` symbol in the API reference.
* 📝 Add a plain-English style guide to `CONTRIBUTING.md`.

### Internal

* 🐛 De-flake `test_lock_reentrant_from_thread` on Python 3.12. Fixes [#105](https://github.com/grelinfo/grelmicro/issues/105).
* 🔧 Add keywords to `pyproject.toml` for PyPI discovery.

## 0.14.0 - 2026-04-21

### Features

* ✨ Add pluggable `RateLimiter` algorithms via the `algorithm=` parameter: `TokenBucket` and `GCRA`. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `MemoryTokenBucket`, a standalone synchronous token-bucket primitive. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `RateLimitFilter`, a `logging.Filter` with configurable `key_mode`. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `DuplicateFilter`, a `logging.Filter` that caps repeated records per key with optional TTL. PR [#94](https://github.com/grelinfo/grelmicro/pull/94).
* ✨ `HealthRegistry` now logs every unhealthy path at `WARNING` (`ERROR` for unexpected exceptions). PR [#92](https://github.com/grelinfo/grelmicro/pull/92).

### Deprecations

* 🗑️ `RateLimiter(name, limit=..., window=...)` is deprecated. Use `RateLimiter(name, algorithm=GCRA(limit=..., window=...))` instead. Will be removed in 0.15.0. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).

### Docs

* 📝 Add [`CONTRIBUTING.md`](https://github.com/grelinfo/grelmicro/blob/main/CONTRIBUTING.md) with repo conventions. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* 📝 Add a "Choosing an algorithm" guide for `TokenBucket` vs `GCRA` in the [Rate Limiter](resilience/rate-limiter.md) docs. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* 📝 Surface `THIRD_PARTY_NOTICES.md` in the docs site. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).

### Security

* 🔒️ Harden CI supply chain: pin all Actions to SHAs, close `run:` injection vectors, add zizmor workflow-lint, restrict Dependabot auto-merge to uv patch/minor updates. PRs [#95](https://github.com/grelinfo/grelmicro/pull/95), [#100](https://github.com/grelinfo/grelmicro/pull/100), [#101](https://github.com/grelinfo/grelmicro/pull/101).

### Internal

* ⬆️ Bump `pydantic` to 2.13.0, `opentelemetry-api` / `opentelemetry-sdk` to 1.41.0, `pytest` to 9.0.3, `ruff` to 0.15.10, `ty` to 0.0.29, `fastapi` to 0.135.3, `uvicorn` to 0.44.0. PR [#99](https://github.com/grelinfo/grelmicro/pull/99).
* ⬆️ Bump `pydantic-extra-types` from 2.11.1 to 2.11.2. PR [#89](https://github.com/grelinfo/grelmicro/pull/89).
* ⬆️ Pre-commit `ruff` autoupdate (v0.15.9 → v0.15.11). PR [#91](https://github.com/grelinfo/grelmicro/pull/91).
* ⬆️ Bump `codecov/codecov-action` to v6. PR [#96](https://github.com/grelinfo/grelmicro/pull/96).
* ⬆️ Bump `astral-sh/setup-uv` to v8. PR [#97](https://github.com/grelinfo/grelmicro/pull/97).
* ⬆️ Bump `dependabot/fetch-metadata` to v3. PR [#98](https://github.com/grelinfo/grelmicro/pull/98).

## 0.13.0 - 2026-04-08

### Features

* ✨ Add `RateLimiter.peek(key)`: check rate limit state without consuming tokens. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).
* ✨ Add `RateLimiter.reset(key)`: delete rate limit state for a key, restoring full quota. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).
* ✨ Add `fail_open` parameter to `RateLimiter`: return allowed result on backend errors instead of propagating exceptions. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).

## 0.12.0 - 2026-04-07

### Features

* ✨ Add `health` module with health check registry, concurrent checker execution, and FastAPI integration for liveness/readiness probes. PR [#84](https://github.com/grelinfo/grelmicro/pull/84).

### Internal

* ⬆️ Bump orjson from 3.11.7 to 3.11.8. PR [#72](https://github.com/grelinfo/grelmicro/pull/72).
* ⬆️ Bump ty from 0.0.26 to 0.0.27. PR [#74](https://github.com/grelinfo/grelmicro/pull/74).
* ⬆️ Update uv-build requirement from `<0.10.0` to `<0.12.0`. PR [#75](https://github.com/grelinfo/grelmicro/pull/75).
* 👷 Add build provenance attestations and wheel verification to release pipeline.
* ♻️ Pre-release cleanup: add health/json to overview, fix style inconsistencies, remove stale branches.

## 0.11.0 - 2026-04-03

### Breaking Changes

* 💥 **Logging**: split `caller` into separate `logger` (logger name) and `caller` (`function:line`) fields. `caller` is now opt-in via `GREL_LOG_CALLER_ENABLED` (default: `False`), following slog/zap/zerolog/Caddy conventions. Uvicorn formatter never includes `caller`.
* 💥 **Cache**: replace `TTLCache` `serializer`/`deserializer` callable pair with a single `serializer` accepting a `CacheSerializer` protocol object. Use `JsonSerializer()`, `PydanticSerializer(Model)`, or `PickleSerializer()` instead.

### Features

* ✨ Add `GREL_LOG_CALLER_ENABLED` setting to opt in to caller info (`function:line`) in log records. Disabled by default for cleaner logs and better performance.
* ✨ Add `logger` field (logger name, e.g., `myapp.api`) to all log records across all backends and formats.
* ✨ Add `grelmicro.json` module with fast JSON serialization using `orjson` when available, with automatic fallback to stdlib `json`.

## 0.10.0 - 2026-04-02

### Features

* ✨ Add `RateLimiter` to the `resilience` module: Redis-backed sliding-window rate limiting using the GCRA algorithm. Includes `RateLimitResult` with fields mapping to IETF rate limit headers, weighted requests via `cost` parameter, and `RateLimitExceededError`.

### Removals

* 🗑️ Remove deprecated `UvicornJSONFormatter` and `UvicornAccessJSONFormatter`. Use `UvicornFormatter` and `UvicornAccessFormatter` instead (deprecated since 0.9.1).

### CI

* ⚡ Migrate PyPI publishing from API token to OIDC trusted publishing.

## 0.9.1 - 2026-04-01

### Deprecations

* 🗑️ **`UvicornJSONFormatter` and `UvicornAccessJSONFormatter` are deprecated.** Use `UvicornFormatter` and `UvicornAccessFormatter` instead. The new formatters respect `GREL_LOG_FORMAT` instead of always producing JSON. Old names kept as aliases with `DeprecationWarning`.

## 0.9.0 - 2026-04-01

### Breaking Changes

* 💥 **`GREL_LOG_FORMAT` default changed from `JSON` to `AUTO`.** In production (non-TTY), behavior is identical (JSON output). In local dev (TTY), output switches to human-readable `TEXT` with colors. Set `GREL_LOG_FORMAT=JSON` explicitly to restore the previous default.

### Features

* ✨ Add `AUTO` log format (new default): detects TTY and selects `TEXT` (terminal) or `JSON` (piped/CI).
* ✨ Add `LOGFMT` log format: key-value pairs following the [logfmt](https://brandur.org/logfmt) convention, 30-40% smaller than JSON.
* ✨ Add `PRETTY` log format: multi-line indented output with structured error rendering, inspired by Rust [tracing Pretty](https://docs.rs/tracing-subscriber/latest/tracing_subscriber/fmt/format/struct.Pretty.html).
* ✨ Enhanced `TEXT` format: now includes extra context fields as `key=value` pairs and supports ANSI colors.
* ✨ Add `NO_COLOR` / `FORCE_COLOR` environment variable support following [no-color.org](https://no-color.org) standard.

## 0.8.0 - 2026-04-01

### Breaking Changes

* 💥 **Backend imports moved to submodules.** Use `from grelmicro.sync.redis import RedisSyncBackend` instead of `from grelmicro.sync import RedisSyncBackend`. Same for all sync, cache, and logging backends. See [Import Strategy](architecture/imports.md).

### Features

* ✨ Add Uvicorn JSON formatters (`UvicornJSONFormatter`, `UvicornAccessJSONFormatter`) for structured logging via `dictConfig`.

## 0.7.0 - 2026-03-31

### Breaking Changes

* 💥 **Logging JSON format redesigned** to follow industry standards (slog, zap, zerolog):
    * `logger` renamed to `caller`
    * `thread` removed
    * `ctx` removed: extra fields are now flat at the top level
    * `exception` replaced by structured `error` object (`type`, `message`, `stack`)

### Features

* ✨ Add `tracing` module with `@instrument` decorator, `span()` context manager, and `add_context()` for unified logging and OTel instrumentation (inspired by Rust's `tracing` crate).

### Performance

* ⚡ **Logging**: Up to +23% throughput across all backends.
* ⚡ Use `OrderedDict` for O(1) LRU operations in `TTLCache`.

### Refactors

* ♻️ Extract shared Redis config into `grelmicro/_redis.py`.
* ♻️ Make `TTLCache` generic and add `Doc` annotations.
* ♻️ Extract context stack into `grelmicro/_context.py` to decouple logging from tracing.
* ♻️ Filter private (`_`-prefixed) attributes from stdlib JSON log output.
* ♻️ Widen `@instrument(skip=...)` type from `set[str]` to `AbstractSet[str]`.

### Removals

* 🗑️ `Synchronization` protocol removed. Use `SyncPrimitive` instead (deprecated since 0.6.0).
* 🗑️ `ResilienceException` removed. Use `ResilienceError` instead (deprecated since 0.6.0).
* 🗑️ The `token` parameter on lock errors removed (deprecated since 0.6.0).
* 🗑️ The `sync` parameter on `interval()` removed (deprecated since 0.6.0).
* 🗑️ The `scheduled()` decorator removed (deprecated since 0.6.0).

## 0.6.0 - 2026-03-30

### Deprecations

* 🗑️ `Synchronization` protocol renamed to `SyncPrimitive`. The old name still works but emits a `DeprecationWarning`. Will be removed in 0.7.0.
* 🗑️ `ResilienceException` renamed to `ResilienceError`. The old name still works but emits a `DeprecationWarning`. Will be removed in 0.7.0.
* 🗑️ The `token` parameter on `LockAcquireError`, `LockReleaseError`, and `LockNotOwnedError` is deprecated. Tokens are no longer included in error messages for security. Will be removed in 0.7.0.
* 🗑️ The `sync` parameter on `interval()` for `TaskLock` and `LeaderElection` is deprecated. Use `max_lock_seconds` and `leader` parameters instead. Will be removed in 0.7.0.
* 🗑️ The `scheduled()` decorator is deprecated. Use `interval()` with `max_lock_seconds` or `leader` instead. Will be removed in 0.7.0.

### Features

* ✨ Add in-memory [TTL cache](cache.md) with LRU eviction, per-key stampede protection, and `@cached` decorator.
* ✨ Add `RedisCacheBackend` for distributed cache storage.
* ✨ Add cache statistics via `CacheInfo` (hits, misses, evictions, stampedes).
* ✨ Add Kubernetes sync backend using Lease resources (`pip install grelmicro[kubernetes]`).
* ✨ Add SQLite sync backend for home lab and local testing (`pip install grelmicro[sqlite]`).

### Security

* 🔒️ Remove token values from lock error messages to prevent leaking in logs.
* 🔒️ Upgrade `requests` to 2.33.0 (CVE fix in transitive dependency).

### Refactors

* ♻️ Unify error hierarchy under `GrelmicroError` base class. All module errors (`SyncError`, `ResilienceError`, `LoggingError`, `TaskError`, `CacheError`) now share a common base.
* ♻️ Use server-side timestamps and native Lease fields in sync backends.
* ♻️ Simplify token generation from UUID-based to string concatenation.
* ♻️ Harden TaskLock token nonce and error handling.

### Internal

* ✅ Achieve 100% library code coverage.
* 💚 Fix flaky integration test timeout in CI.
* ⬆️ Bump dependencies and fix ty v0.0.26 type errors.

### Docs

* 📝 Add [cache module](cache.md) documentation with usage guide and API reference.
* 📝 Add [Kubernetes Backend Architecture](architecture/kubernetes.md) page.
* 📝 Add [SQLite Backend Architecture](architecture/sqlite.md) page.
* 📝 Add backend comparison matrix to [Synchronization](sync.md#backend) guide.
* 📝 Rewrite README with project vision.

## 0.5.0 - 2026-03-17

### Breaking Changes

* 💥 Add namespace prefix to sync primitive backend keys (`lock:`, `tasklock:`, `leader:`). See [Migration Guide](#migration-guide) below.

### Features

* ✨ Add `TaskLock.from_thread` thread-safe adapter. PR [#57](https://github.com/grelinfo/grelmicro/pull/57).
* ✨ Add specific lock error classes (`LockAcquireError`, `LockReleaseError`, `LockLockedCheckError`, `LockOwnedCheckError`, `LockReentrantError`). PR [#57](https://github.com/grelinfo/grelmicro/pull/57).

### Refactors

* ♻️ Consolidate distributed lock and leader gating into the `interval()` decorator via `max_lock_seconds` and `leader` parameters. PR [#54](https://github.com/grelinfo/grelmicro/pull/54).

### Docs

* 📝 Add [Synchronization Architecture](architecture/sync.md) page. PR [#57](https://github.com/grelinfo/grelmicro/pull/57).

### Internal

* ⬆️ Bump redis, fastapi, pydantic, and pydantic-settings. PR [#55](https://github.com/grelinfo/grelmicro/pull/55).
* ⬆️ Update pre-commit hooks. PR [#50](https://github.com/grelinfo/grelmicro/pull/50).

### Migration Guide

#### Namespace-Prefixed Backend Keys

Prior versions used the `name` parameter directly as the backend key. Now each primitive adds a type-specific prefix:

| Primitive | Name | Backend Key |
|---|---|---|
| `Lock("my-resource")` | `my-resource` | `lock:my-resource` |
| `TaskLock("cleanup")` | `cleanup` | `tasklock:cleanup` |
| `LeaderElection("main")` | `main` | `leader:main` |

Existing locks stored in Redis or PostgreSQL will no longer match after upgrading. A running instance on the old version and one on the new version will **not** see each other's locks.

Upgrade all running instances together so they use the same key format. Old keys expire automatically via their lease duration (Redis `PEXPIRE` / PostgreSQL `expire_at`).

## 0.4.1 - 2026-03-13

### Docs

* 📝 Add Task Lock to synchronization primitives guide.

### Internal

* ⬆️ Bump actions/checkout to v6 and astral-sh/setup-uv to v7.

## 0.4.0 - 2026-03-13

### Features

* ✨ Add `TaskLock` for ShedLock-style distributed task locking.
* ✨ Add `GREL_LOG_TIMEZONE` support for configurable timezone in logging output.
* ✨ Add OpenTelemetry trace context injection into log records.
* ✨ Add `structlog` as alternative logging backend.
* ✨ Add configurable JSON serializer (`json` / `orjson`) for logging.

### Docs

* 📝 Add logging benchmark and performance documentation.

### Internal

* ⬆️ Bump orjson from 3.11.5 to 3.11.6. PR [#51](https://github.com/grelinfo/grelmicro/pull/51).
* ⬆️ Bump freezegun from 1.5.2 to 1.5.5. PR [#33](https://github.com/grelinfo/grelmicro/pull/33).

## 0.3.2 - 2026-01-27

### Internal

* 👷 Migrate from mypy to Astral ty for type checking. PR [#45](https://github.com/grelinfo/grelmicro/pull/45).
* 🔧 Add Python 3.14 support. PR [#47](https://github.com/grelinfo/grelmicro/pull/47).
* 🔧 Switch build system to `uv_build`. PR [#49](https://github.com/grelinfo/grelmicro/pull/49).
* 💚 Simplify CI and release workflow. PR [#24](https://github.com/grelinfo/grelmicro/pull/24).

## 0.3.1 - 2025-06-05

### Docs

* 📝 Add resilience patterns section and update links in README and index.

### Internal

* 💚 Fix release pipeline and GitHub Pages deployment permissions.

## 0.3.0 - 2025-06-05

### Features

* ✨ Add Circuit Breaker resilience pattern. PR [#18](https://github.com/grelinfo/grelmicro/pull/18).

### Docs

* 📝 Refactor code examples to use snippets.

### Internal

* 👷 Add Dependabot configuration for weekly updates.
* 🔒️ Fix workflow permission issues. PR [#21](https://github.com/grelinfo/grelmicro/pull/21), [#22](https://github.com/grelinfo/grelmicro/pull/22).

## 0.2.3 - 2024-12-04

### Features

* ✨ Add Redis key prefix support to avoid conflicts in shared instances.
* ✨ Add Redis and PostgreSQL settings management from environment variables.

## 0.2.2 - 2024-11-28

### Features

* ✨ Add PostgreSQL backend configuration from environment variables.

### Internal

* 🐛 Fix release workflow. PR [#7](https://github.com/grelinfo/grelmicro/pull/7), [#9](https://github.com/grelinfo/grelmicro/pull/9).

## 0.2.1 - 2024-11-26

### Internal

* 💚 Set up release workflow with version tagging.

## 0.2.0 - 2024-11-26

First public release.

### Features

* ✨ Add distributed `Lock` with lease-based expiration.
* ✨ Add `LeaderElection` for single-leader task execution.
* ✨ Add `IntervalTask` scheduler for periodic tasks with synchronization support.
* ✨ Add Redis, PostgreSQL, and in-memory synchronization backends.
* ✨ Add logging module with JSON and TEXT formatting via `GREL_LOG_LEVEL` and `GREL_LOG_FORMAT` environment variables.

### Docs

* 📝 Add MkDocs documentation site with Material theme.

### Internal

* 👷 Add unified CI workflow with linting, testing, and coverage.
