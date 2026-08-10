# Roadmap

What is planned, and what waits for demand. Every item is additive. No dates are promised.

grelmicro ships on the `0.x` line. **Next** means the next few releases on that line, not work held back behind a 1.0 bump. For what ships today, read the [capability matrix](capabilities.md).

An entry that has an issue links it. The issue carries the discussion, this page carries the shape. A pull request that ships one of them updates this page in the same change, so nothing here outlives what it describes.

## Next

Small, high-demand, additive.

- **Rate-limit response headers**: a helper that renders the [`RateLimit` and `RateLimit-Policy`](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/) headers, and the legacy `X-RateLimit-*` ones, from a `RateLimitResult`. Pairs with the limiter and `wait()`.
- **Named cron helpers**: `@tasks.daily(at=...)`, `@tasks.hourly()`, and `@tasks.weekly()` over the existing cron engine, for schedules a stranger reads at a glance.
- **Circuit breaker state-change callbacks**: `on_open`, `on_half_open`, and `on_close` hooks so an operator can react to a trip, not only observe it in metrics.
- **Observability depth**: metric exemplars, lock-acquire latency, and deeper [task metrics](https://github.com/grelinfo/grelmicro/issues/691) (runs, duration, schedule drift, skip reasons).

## Later

Demand-gated. Built when a concrete need shows up.

- **Fleet-wide retry budgets**: cap the retry-to-call ratio across replicas through distributed backends.
- **Request hedging on Shield**: fire a backup attempt after a latency threshold, take the fastest, cancel the loser.
- **Distributed `Semaphore`**: a new class alongside `Lock` and `ReadWriteLock`. No hooks needed.
- **Adaptive `Bulkhead`**: the CUBIC machinery inside Shield, exposed as `Bulkhead.adaptive()`.
- **Deadline propagation**: a contextvar deadline that `Timeout`, `Retry`, and `Shield` respect.
- **Resilience composition**: a [single decorator that applies the patterns in the recommended order](https://github.com/grelinfo/grelmicro/issues/689) whatever order you list them, a horizontal `compose()` for assembling a policy list at runtime, and slow-call rate as a trip input to the failure-rate breaker.
- **Framework depth**: `Depends()` helpers, an ASGI per-route rate-limit middleware, and a first-class Litestar integration. The pure-ASGI `GrelmicroMiddleware` already runs on Litestar today.
- **Strict per-key outbox ordering**: opt-in head-of-line semantics, so two messages sharing a `key` are delivered in order.
- **Provider pool metrics**: connection-pool gauges per provider.
- **More backends**: MySQL/MariaDB, MongoDB, etcd/ZooKeeper, an outbox adapter for SQLite, and [a file and an object-store cache](https://github.com/grelinfo/grelmicro/issues/694).
- **Free-threading and multiple event loops**: [run under a free-threaded interpreter](https://github.com/grelinfo/grelmicro/issues/693), and let one process drive components from more than one loop.
- **Multi-window rate limits and task pause/resume**: additive features with lower urgency.
- **Uniform admission guard**: a `@guard(on_reject=...)` decorator over the shared `AdmissionError` base (was issue #356).
- **Saga helpers**: docs-first recipes for orchestrating a multi-step workflow on `Tasks`, `TaskLock`, and the [outbox](outbox.md), then a helper if demand shows (was issue #175).
- **[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) problem-detail responses**: error-to-response mapping for the FastAPI integration (was issue #78).
- **Project starter template**: a `copier` or `cookiecutter` starter wiring one provider, health, and one pattern (was issue #179).
