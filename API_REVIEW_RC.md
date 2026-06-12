# API review for the 1.0 release candidate

A full review of the public API surface against the goal: the best Python
library for distributed systems, simple to use, solving complex problems.
Review date: 2026-06-11, on branch `externalized-config` (0.27.0 plus the
unreleased `grelmicro.config` module). Method: the frozen API snapshot
(`tests/__snapshots__/test_public_api`), four deep module reviews
(coordination and task, resilience, cache and clock and config, app and
observability), and a fresh scan of the 2026 peer landscape.

## Verdict

The surface is in excellent shape. Eleven patterns, five backends, no open
matrix cells, one consistent idiom (`name` first, keyword-only params,
`backend=` injection, `from_config`, single-token factories), and a freeze
guard test that makes drift deliberate. No Python peer has this breadth, and
several supposed gaps from earlier analyses are already closed (decorrelated
jitter ships in `ExponentialBackoff`, tags and batch ops ship in cache,
`max_wait` ships in `Bulkhead`).

Three things genuinely stand between here and an RC tag. They are decisions
that become frozen contracts at 1.0, not feature count:

1. The silent in-memory fallback on `CircuitBreaker` and `RateLimiter`.
2. The `ExternalConfig` reload keying, which today cannot reach the two
   patterns operators most want to tune live.
3. The FastAPI ambient-scope footgun, which is the root cause of item 1
   biting in practice.

Everything else is additive polish or post-1.0 roadmap.

## What is already best-in-class

Lead with these at launch, no changes needed:

- **Fencing tokens on every lock backend.** Still the only Python library
  with first-class fencing. Needs one docs addition (see polish list).
- **Distributed-safe cron with zero broker.** At-most-once per fire on
  Memory, Redis, Postgres, SQLite. No peer has this, including APScheduler 4.
- **The Shield composite.** Timeout, adaptive rate, retry budget, cache, and
  fallback as one primitive. Novel in Python.
- **Health checks.** Single-flight, per-check cache, critical versus
  non-critical, Kubernetes-aligned routers. Exceeds the dedicated libraries.
- **The test story.** `micro.override(...)`, `record(backend)`,
  `VirtualClock`, and a clock seam through every time-driven pattern.
- **The plugin surface.** Entry-point discovery with a complete worked
  third-party example (`examples/third-party-adapter`).
- **Live reconfiguration.** `Reconfigurable` on every pattern is rare, and
  the in-flight `ExternalConfig` turns it into a headline feature.

## Decisions to settle before RC (the real gate)

### 1. The implicit memory fallback is a frozen contract. Decide it on purpose.

`Lock` and `Cache` raise `NoActiveAppError` when no backend resolves.
`CircuitBreaker` and `RateLimiter` instead fall back to a process-global
implicit memory adapter (`resilience/circuitbreaker/__init__.py:802`,
`resilience/ratelimiter/__init__.py:552`). That asymmetry is defensible (the
README one-liner depends on it), but it has a failure mode: a user who wires
`RateLimiters(redis)` and then calls the limiter from a FastAPI handler
outside the ambient scope gets a **silent per-process limiter** where they
believe they have a fleet-wide one. That is a production incident, not an
inconvenience. After 1.0, changing this behavior is breaking.

Proposal, keeping the zero-config one-liner alive:

- Keep the implicit memory adapter when **no app has ever been entered** in
  the process (the standalone path).
- Once an app with a registered `RateLimiters` or `CircuitBreakers`
  component is active anywhere in the process, ambient-miss resolves through
  it or raises `OutOfContextError` instead of silently degrading.
- Log one warning the first time the implicit adapter activates.

### 2. ExternalConfig must reach CircuitBreaker and RateLimiter.

The branch keys live reload on `_track_reconfigure(env_prefix)`, so only
instances that resolved from the environment participate. `CircuitBreaker`
and `RateLimiter` dropped env loading in #163, so the two patterns with the
strongest ops story (raise a rate limit during an incident, loosen a breaker
threshold) can never be reloaded. The wiring so far confirms it: only `Lock`
is tracked.

Proposal: register every named `Reconfigurable` under its derived
name-as-namespace key (`GREL_RATELIMITER_API_*`, `GREL_CIRCUITBREAKER_PAYMENTS_*`)
whether or not it was built from env vars. The env prefix is already the
universal addressing scheme, so a mounted ConfigMap can address any pattern
by name. Instances built via `from_config` stay opted out (the declarative
path is static by design), with that opt-out stated in the docs. This is a
contract decision: once `ExternalConfig` ships, its addressing scheme is
frozen.

Three smaller items on the same module, all before it ships:

- **`await external_config.reload()`** as a public method. Tests and ops
  runbooks need a deterministic trigger, not a 10-second poll wait.
- **Error contract on `ConfigBackend.load`.** State what the adapter raises
  on unreadable source versus invalid data, and that `ExternalConfig` keeps
  the last good config and logs on either.
- **Never log values.** The reload warning path logs validation errors that
  can embed the offending value (a Secret mount makes this a credential
  leak). Log key names and field locations only.

### 3. Fix the ambient scope in FastAPI handlers.

Request handlers run in their own task, outside `async with micro:`, so
every doc and demo tells users to pass `backend=` explicitly inside
handlers (issue #328). That is the single largest source of wiring
boilerplate in the README example, and it is what arms the silent-fallback
gun in item 1.

Proposal: a pure-ASGI middleware that propagates the active app into the
request task.

```python
from grelmicro.integrations.fastapi import GrelmicroMiddleware

app = FastAPI(lifespan=lifespan)
app.add_middleware(GrelmicroMiddleware, app=micro)
```

With it, `Lock("cart")`, `RateLimiter.sliding_window("api", ...)`, and
`@cached` resolve ambiently inside handlers exactly as they do in tasks. The
README example loses five `backend=` lines and the "handlers are special"
caveat. This is the biggest single DX unlock available and it is additive.

## Ship in 1.0 (additive, but first impressions count)

Reviewers will compare each module against the specialist they know. These
four close the comparisons that would otherwise dominate a launch thread:

### Lock lease lifecycle

`Lock` today has `acquire()` (waits forever) and `acquire_nowait()`. Peers
(redis-py `Lock`, python-redis-lock, kazoo) all have a bounded wait and an
extend. Both are additive kwargs and methods:

```python
async def acquire(self, *, timeout: Seconds | None = None) -> LockHandle: ...
    # None keeps today's wait-forever. Raises TimeoutError at the deadline.

async def extend(self) -> LockHandle: ...
    # Renew the lease for another lease_duration. Raises LockNotOwnedError
    # when the lease was lost. Returns the handle (token unchanged).
```

`TaskLock` gets the same public `refresh()` (the private `do_reacquire`
already exists) so a task body that outruns `max_lock_seconds` can keep its
claim instead of silently going concurrent.

### Contention jitter on coordination retries

`Lock` and `LeaderElection` retry on a fixed `retry_interval`, so fifty
replicas hammer the backend in lockstep at startup. Add `retry_jitter`
(default a modest 0.1 factor) to `LockConfig` and `LeaderElectionConfig`.
One field, frozen-config friendly, and on-brand for a library that already
ships decorrelated jitter in retry backoffs.

### Scheduler introspection

`@cron` and `@interval` tasks expose nothing at runtime. Two read-only
properties close the gap with APScheduler and feed health and metrics:

```python
task.next_fire_time  # datetime | None
task.last_fire       # FireInfo | None (started_at, outcome, duration)
```

### Fencing docs

The capability exists, the contract is implicit. Add to
`docs/architecture/coordination.md`: a per-backend monotonicity table
(Memory, Redis, Postgres, SQLite, Kubernetes, each with its scope) and one
worked resource-side check (`WHERE fence < :token`). Fencing is the
correctness differentiator, so the proof must be one link away.

## Polish (small, do alongside the above)

- `testing.record` drops positional arguments (`testing.py:133` stores
  kwargs only). Capture `args` on `Call` so assertions can match them.
- The sync `@cached` wrapper fails with an opaque `AttributeError` when the
  backend never captured a loop. Raise a `RuntimeError` that says "open the
  backend with `async with micro:` first".
- `Match` predicates returning non-bool values are silently truthy. Coerce
  and warn once, and add `Match.explain()` returning the human-readable
  matcher tree for debugging.
- The `resolve_config_from_mapping` filter drops typoed keys silently. Log
  at debug which prefixed keys matched no field, so a ConfigMap typo is
  diagnosable.

## Post-1.0 roadmap (publish it, do not build it now)

All purely additive. Publishing the list makes the gaps read as intentional.
Ordered by differentiation, not effort:

1. **`@idempotent` decorator.** Stripe-style idempotency keys on the
   existing `TTLCache` machinery: check, execute once, replay the stored
   response. No Python library owns this and grelmicro already has every
   ingredient (cache, locks, serializers). The flagship 1.1 candidate.
2. **Fleet-wide retry budgets.** Cap the retry-to-call ratio across
   replicas through the existing distributed backends. No peer can build
   this, because no peer has the backends.
3. **Request hedging on Shield.** Fire a backup attempt after a latency
   threshold, take the fastest, cancel the loser. The one remaining gap
   versus Polly.
4. **Distributed `Semaphore`, then read-write locks.** Already decided in
   ROAD_TO_1.0 section 4. New classes on `LockBackend`, no hook needed.
5. **Adaptive `Bulkhead`.** The CUBIC machinery inside Shield, exposed as
   `Bulkhead.adaptive()`.
6. **Deadline propagation.** A contextvar deadline that `Timeout`, `Retry`,
   and `Shield` respect, gRPC-style.
7. **Framework depth.** `Depends()` helpers, an ASGI per-route rate-limit
   middleware, Litestar and FastStream integration. All follow naturally
   once the context middleware (gate item 3) exists.
8. **Observability depth.** Provider pool metrics, lock acquire latency,
   optional provider health auto-registration, metric exemplars.
9. **Backends.** Valkey, MySQL/MariaDB, MongoDB, then etcd/ZooKeeper, per
   ROAD_TO_1.0 section 4b.
10. **Multi-window rate limits and task pause/resume.** Real asks, lower
    urgency, both additive.

## Reviewed and rejected

Settled negatives, recorded so they are not relitigated:

- **A `Compose`/`Policy` pipeline object for resilience stacking.** The
  documented decorator order plus Shield covers the need. A third
  composition idiom would dilute the one-primitive-per-pattern story.
  Revisit only on demonstrated user demand.
- **Making `RateLimiter`'s config optional.** There is no sensible default
  algorithm. Requiring `config` or a named factory is the honest API.
- **Negative caching as a dedicated `negative_ttl`.** The `skip` predicate
  and a short TTL cover it today, `stale_ttl` covers the error side.
  Additive if demand appears.
- **Retry test toggles (stamina-style `disable()`).** `reconfigure()` and
  `micro.override(...)` already do this. Document the recipe instead of
  adding API.
- **Renaming `ExternalConfig`.** The name states the pattern
  (Externalized Configuration) and matches the docs vocabulary. Keep it.

## Recommended order

| # | Item | Why this order |
| - | ---- | -------------- |
| 1 | ExternalConfig keying + `reload()` + log redaction | On this branch now, contract freezes when it ships |
| 2 | Implicit-fallback decision on breaker/limiter | Behavioral contract, breaking to change after 1.0 |
| 3 | FastAPI context middleware | Unlocks ambient DX, defuses item 2 in practice |
| 4 | `Lock.acquire(timeout=)`, `Lock.extend()`, `TaskLock.refresh()` | Closes the redis-py comparison |
| 5 | Coordination retry jitter | One config field, fleet-scale correctness |
| 6 | Scheduler introspection properties | Closes the APScheduler comparison |
| 7 | Fencing docs table + polish list | Cheap, sharpens the differentiators |
| 8 | Publish the post-1.0 roadmap section | Gaps become intent |

Items 1 through 7 are all additive or unreleased, so none of them reopens
the API freeze. After them, the surface is RC-ready and every launch-thread
comparison ("why not redis-py / APScheduler / tenacity / cashews") has a
one-line answer.
