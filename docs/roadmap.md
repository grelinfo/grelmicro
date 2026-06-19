# Roadmap

Post-1.0 items planned for future releases. All are purely additive. No dates are promised.

- **Fleet-wide retry budgets**: cap the retry-to-call ratio across replicas through distributed backends.
- **Request hedging on Shield**: fire a backup attempt after a latency threshold, take the fastest, cancel the loser.
- **Distributed `Semaphore`, then read-write locks**: new classes on `LockBackend`. No hooks needed.
- **Adaptive `Bulkhead`**: the CUBIC machinery inside Shield, exposed as `Bulkhead.adaptive()`.
- **Deadline propagation**: a contextvar deadline that `Timeout`, `Retry`, and `Shield` respect.
- **Framework depth**: `Depends()` helpers, an ASGI per-route rate-limit middleware, Litestar and FastStream integration.
- **Observability depth**: provider pool metrics, lock acquire latency, metric exemplars.
- **More backends**: MySQL/MariaDB, MongoDB, etcd/ZooKeeper.
- **Multi-window rate limits and task pause/resume**: additive features with lower urgency.
- **Uniform admission guard**: a `@guard(on_reject=...)` decorator over the shared `AdmissionError` base (was issue #356).
- **Saga and transactional outbox helpers**: docs-first recipes on `Tasks` plus `TaskLock`, then a helper if demand shows (was issue #175).
- **RFC 9457 problem-detail responses**: error-to-response mapping for the FastAPI integration (was issue #78).
- **Project starter template**: a `copier` or `cookiecutter` starter wiring one provider, health, and one pattern (was issue #179).
