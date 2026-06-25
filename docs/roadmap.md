# Roadmap

Post-1.0 items planned for future releases. All are purely additive. No dates are promised.

## Next

The first post-1.0 cycle: small, high-demand, additive.

- **Rate-limit response headers**: a helper that renders the `RateLimit-*` and `X-RateLimit-*` headers (RFC 9211) from a `RateLimitResult`, pairing with the limiter and `wait()`.
- **Named cron helpers**: `@tasks.daily(at=...)`, `@tasks.hourly()`, and `@tasks.weekly()` over the existing cron engine, for schedules a stranger reads at a glance.
- **FastAPI Idempotency-Key integration**: map the Idempotency pattern onto the HTTP `Idempotency-Key` header in the FastAPI integration.
- **Circuit breaker state-change callbacks**: `on_open`, `on_half_open`, and `on_close` hooks so an operator can react to a trip, not only observe it in metrics.
- **Observability depth**: metric exemplars and lock-acquire latency.

## Later

Demand-gated. Built when a concrete need shows up.

- **Fleet-wide retry budgets**: cap the retry-to-call ratio across replicas through distributed backends.
- **Request hedging on Shield**: fire a backup attempt after a latency threshold, take the fastest, cancel the loser.
- **Distributed `Semaphore`, then read-write locks**: new classes on `LockBackend`. No hooks needed.
- **Adaptive `Bulkhead`**: the CUBIC machinery inside Shield, exposed as `Bulkhead.adaptive()`.
- **Deadline propagation**: a contextvar deadline that `Timeout`, `Retry`, and `Shield` respect.
- **Resilience composition**: a horizontal `compose()` for assembling a policy list at runtime, plus slow-call rate as a trip input to the failure-rate breaker.
- **Framework depth**: `Depends()` helpers, an ASGI per-route rate-limit middleware, Litestar and FastStream integration.
- **Provider pool metrics**: connection-pool gauges per provider.
- **More backends**: MySQL/MariaDB, MongoDB, etcd/ZooKeeper.
- **Multi-window rate limits and task pause/resume**: additive features with lower urgency.
- **Uniform admission guard**: a `@guard(on_reject=...)` decorator over the shared `AdmissionError` base (was issue #356).
- **Saga and transactional outbox helpers**: docs-first recipes on `Tasks` plus `TaskLock`, then a helper if demand shows (was issue #175).
- **RFC 9457 problem-detail responses**: error-to-response mapping for the FastAPI integration (was issue #78).
- **Project starter template**: a `copier` or `cookiecutter` starter wiring one provider, health, and one pattern (was issue #179).
