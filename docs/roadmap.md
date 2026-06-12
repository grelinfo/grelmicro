# Roadmap

Post-1.0 items planned for future releases. All are purely additive. No dates are promised.

- **Fleet-wide retry budgets**: cap the retry-to-call ratio across replicas through distributed backends.
- **Request hedging on Shield**: fire a backup attempt after a latency threshold, take the fastest, cancel the loser.
- **Distributed `Semaphore`, then read-write locks**: new classes on `LockBackend`. No hooks needed.
- **Adaptive `Bulkhead`**: the CUBIC machinery inside Shield, exposed as `Bulkhead.adaptive()`.
- **Deadline propagation**: a contextvar deadline that `Timeout`, `Retry`, and `Shield` respect.
- **Framework depth**: `Depends()` helpers, an ASGI per-route rate-limit middleware, Litestar and FastStream integration.
- **Observability depth**: provider pool metrics, lock acquire latency, optional provider health auto-registration, metric exemplars.
- **More backends**: MySQL/MariaDB, MongoDB, etcd/ZooKeeper.
- **Multi-window rate limits and task pause/resume**: additive features with lower urgency.
