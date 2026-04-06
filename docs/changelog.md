# Changelog

## Unreleased

### Features

* ✨ Add `health` module with health check registry, concurrent checker execution, and FastAPI integration for liveness/readiness probes. PR [#84](https://github.com/grelinfo/grelmicro/pull/84).

## 0.11.0 - 2026-04-03

### Breaking Changes

* 💥 **Logging**: split `caller` into separate `logger` (logger name) and `caller` (`function:line`) fields. `caller` is now opt-in via `LOG_CALLER_ENABLED` (default: `False`), following slog/zap/zerolog/Caddy conventions. Uvicorn formatter never includes `caller`.
* 💥 **Cache**: replace `TTLCache` `serializer`/`deserializer` callable pair with a single `serializer` accepting a `CacheSerializer` protocol object. Use `JsonSerializer()`, `PydanticSerializer(Model)`, or `PickleSerializer()` instead.

### Features

* ✨ Add `LOG_CALLER_ENABLED` setting to opt in to caller info (`function:line`) in log records. Disabled by default for cleaner logs and better performance.
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

* 🗑️ **`UvicornJSONFormatter` and `UvicornAccessJSONFormatter` are deprecated.** Use `UvicornFormatter` and `UvicornAccessFormatter` instead. The new formatters respect `LOG_FORMAT` instead of always producing JSON. Old names kept as aliases with `DeprecationWarning`.

## 0.9.0 - 2026-04-01

### Breaking Changes

* 💥 **`LOG_FORMAT` default changed from `JSON` to `AUTO`.** In production (non-TTY), behavior is identical (JSON output). In local dev (TTY), output switches to human-readable `TEXT` with colors. Set `LOG_FORMAT=JSON` explicitly to restore the previous default.

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
* ✨ Add `LOG_TIMEZONE` support for configurable timezone in logging output.
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
* ✨ Add logging module with JSON and TEXT formatting via `LOG_LEVEL` and `LOG_FORMAT` environment variables.

### Docs

* 📝 Add MkDocs documentation site with Material theme.

### Internal

* 👷 Add unified CI workflow with linting, testing, and coverage.
