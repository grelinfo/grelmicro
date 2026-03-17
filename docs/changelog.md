# Changelog

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
