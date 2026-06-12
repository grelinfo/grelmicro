# Road to 1.0

What remains before grelmicro 1.0. Current release: **0.27.0** (Development
Status: Beta). Tracking issue: [#124](https://github.com/grelinfo/grelmicro/issues/124).

1.0 means one thing above all: **the public API is frozen**. After 1.0 the
project follows standard semver, so no more breaking changes on a minor. Getting
the API right is the gate, not feature count.

## 1. Freeze the public API (the actual gate)

Settle the consistency items surfaced in the API audit, then commit to the
surface. These are the only true 1.0 blockers.

- [x] **`name` mutability.** Read-only property everywhere (all 10 components
      converted). Shipped in #343.
- [x] **Typed errors at the package root.** Rule: each submodule re-exports its
      catchable errors; cross-cutting base errors (`GrelmicroError`,
      `DependencyNotFoundError`, `OutOfContextError`, `SettingsValidationError`)
      re-exported from the top-level `grelmicro` package. `WouldBlockError` and
      `CoordinationBackendError` now ship from `grelmicro.coordination`. Shipped
      in #343.
- [x] **`env_prefix` parity.** Resolved as the singleton exception: `Log`,
      `Trace`, `Metrics` confirmed process-global, now enforced singletons
      (second instance raises), global prefix kept. Shipped in #343.
- [x] **Decorator forms.** Documented in `docs/architecture/decorators.md`.
      Shipped in #343.
- [x] **Provider factory naming.** Settled on the single-token pattern name:
      `circuitbreaker()`, `ratelimiter()`, `leaderelection()` (matches module
      names + kind strings). Shipped in #343.
- [x] Final pass: confirmed no internal names in any `__all__`, every exported
      public symbol has a docstring, `ty` confirms type hints. Shipped in #343.

### Open follow-ups from the #343 freeze

- [x] **`task_lock()` naming.** Renamed to `tasklock()` for consistency with the
      other single-token factory methods. Shipped in #344.
- [x] **Env var prefix casing.** Aligned to the single-token names:
      `GREL_LEADERELECTION_` / `GREL_TASKLOCK_`. Shipped in #346.

## 2. Close the capability matrix — DONE (#349)

Both `Future` cells shipped, so the matrix has no open `Future` at 1.0.

- [x] **`TTLCache` on SQLite** — `SQLiteCacheAdapter`, passes the cache
      conformance suite. Shipped in #349.
- [x] **`CircuitBreaker` on SQLite** — `SQLiteCircuitBreakerAdapter`, the
      Postgres state machine ported Python-side. Single-host multi-process scope
      (documented). Shipped in #349.

## 3. CI reliability (clean and green, every run)

The 0.27.0 release needed several CI re-runs. All three known flaky spots are
fixed and merged in #345.

- [x] **k3s testcontainer flake.** `_wait_for_k3s` now treats any `exec` error
      (the startup 409 "container is not running") as not-ready and keeps polling
      until the timeout.
- [x] **`test_only_one_leader` timing flake.** Counts
      `is_leader_confirmed_within(renew_deadline)` instead of advisory
      `is_leader()`, so a just-demoted worker (stale confirmation) is excluded.
      Ran 15x clean.
- [x] **`test_strategy_gcra_burst_never_exceeds_limit` flake.** Driven through
      `VirtualClock` so the burst is one instant and the result is deterministic
      (`allowed == limit`).
- [x] **Bonus: `clock` pytest fixture.** Added an async `clock` fixture in
      `tests/conftest.py` so tests get a `VirtualClock` without the
      `async with VirtualClock()` boilerplate.

## 4. Decide the feature roadmap (1.0 versus later)

Decided from the market analysis (full write-up in
`ROADMAP_SECTION4_ANALYSIS.md`). The 1.0 gate is the frozen API, so the
decisive lens is whether deferring forces a post-1.0 breaking change or stays
purely additive. Items that only need an **API hook reserved now** are flagged.

**Ship in 1.0**

- [x] **Scheduler cron trigger (`@cron`).** Shipped in #348, and went beyond the
      original scope: durable + distributed via an atomic fire-claim
      (`ScheduleBackend` on Memory/Redis/Postgres/SQLite), at-most-once per fire,
      missed-fire replay + coalesce, `misfire_grace_seconds`. Built-in 5-field
      parser, `zoneinfo`, default UTC. Design rationale + peer audit in
      `CRON_DISTRIBUTED_STUDY.md`. Calendar/seconds-field triggers remain post-1.0.
- [x] **Retry time-based stop (`max_seconds=`).** Shipped in #347. Flat field on
      `RetryConfig`, first-limit-wins with `attempts`, clock-seam driven.
      Removed the "planned" caveat from the comparison page.

**Reserve the API hook at freeze (impl may follow post-1.0)**

- [x] **Bulkhead wait-queue.** Shipped, not just reserved. `max_wait=` is the
      wait parameter (seconds a caller waits for a permit), the default is
      fail-fast (`None`/`0` reject immediately with `BulkheadFullError`), and the
      wait is a real FIFO queue via `asyncio.Semaphore`. Tested for fail-fast,
      acquire-when-freed, and reject-after-timeout.
- [x] **Cache soft-TTL / stale-on-error.** Shipped as `stale_ttl=` on
      `@cached`, `get_or_set`, and `TTLCache.set`. A value keeps a fallback
      copy for `ttl + stale_ttl` seconds (a stale-reserve sidecar, the same
      pattern as XFetch), and a recompute that fails after the TTL serves the
      last good value instead of raising. No `CacheBackend` protocol change was
      needed, so third-party adapters stay forward-compatible. Serve-stale-
      while-revalidate is already covered by `early=`.

**Post-1.0 (all purely additive, no hook needed)**

- [ ] **Coordination primitives.** Distributed `Semaphore` and read-write locks
      as new classes on the existing `LockBackend` protocol. Highest impl risk
      (fenced counting + crash recovery on every backend), no breaking-change
      pressure.
- [ ] **Resilience depth.** Hedging, retry budgets, decorrelated-jitter backoff
      (a new strategy in `resilience/backoffs/`). Niche; no Python peer ships
      these.

State the decision in the changelog or the roadmap issue so users know what 1.0
is and is not.

## 4b. Backend / provider priority (post-API-freeze)

Each capability is a Backend protocol with vendor Adapters resolved via the
Provider factory, so partial per-capability coverage is cheap. Priority order:

- [x] **Valkey** — shipped: `ValkeyProvider` (extra `valkey`), a thin
      `RedisProvider` subclass serving the full Redis adapter column.
- [ ] **MySQL / MariaDB** — the glaring hole in an otherwise complete
      five-capability SQL story; `GET_LOCK` advisory locks + dialect tweaks.
- [ ] **MongoDB** — strongest non-SQL/non-Redis datastore; supported by every
      peer category (APScheduler, limits, Celery).
- [ ] **etcd / ZooKeeper** — consensus stores viable as `Lock` and
      `LeaderElection` backends. Lower priority than the datastores above, but
      in scope as future backends.

Out of scope / later: **Consul** (control-plane store, revisit alongside
etcd/ZooKeeper), Memcached (cache-only, breaks the one-client-many-patterns
pitch), DynamoDB + cloud-native (post-1.0 theme), NATS (messaging, an explicit
non-goal).

## 5. Launch tasks

These are human launch steps, tracked in `LAUNCH_CHECKLIST.md`. Summary:

- [ ] Record the ~30s demo asset and embed it in the README.
- [ ] Enable the OpenSSF Scorecard and SLSA provenance badges.
- [ ] Re-run `docs/benchmarks.md` numbers on a clean machine and date them.
- [ ] Move the changelog `Unreleased` section under the `1.0.0` heading at release.
- [ ] Keep the `Development Status` classifier at `4 - Beta`. Do NOT flip it to
      `5 - Production/Stable` (maintainer decision).
- [ ] Write and schedule the launch posts (Show HN, r/Python, r/FastAPI, dev.to).

## Definition of done for 1.0

- Public API frozen and internally consistent (section 1).
- No open `Future` cells in the capability matrix (section 2).
- CI reliably green with no re-runs needed (section 3).
- Feature scope for 1.0 decided and documented (section 4).
- Launch assets and metadata ready (section 5).
