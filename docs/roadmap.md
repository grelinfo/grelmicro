# Roadmap

Where grelmicro is going. Everything here is additive, and no dates are promised. For what ships today, read the [capability matrix](capabilities.md).

Each heading names a few concrete things. They illustrate the direction, they are not a commitment or a queue.

## The HTTP edge

A pattern you already use should reach the wire without hand-written glue. Today the rate limiter hands you a `RateLimitResult` and you build the 429 yourself. Next: rate limit response headers, problem-detail error responses, admission per route instead of per handler, dependency helpers, and a first-class Litestar integration.

## Resilience that composes and coordinates

The primitives exist. Making them work together is what is left. One decorator should apply them in the recommended order whatever order you list them, and `compose()` should build a policy list at runtime.

Coordination is the other half. Retry budgets, bulkheads, and adaptive limiters are per-process, so ten replicas retry ten times as hard as one. They should share state on the backends you already run. A deadline should propagate through `Timeout`, `Retry`, and `Shield` rather than being restated at each layer, and a merely slow call should have an answer of its own.

## Observability that closes the loop

Every component emits metrics. Next comes depth and reacting. Depth is the numbers you page on: lock acquire latency, schedule drift, skip reasons, pool gauges, and exemplars linking a metric to its trace. Reacting is a circuit breaker that calls your code when it trips, instead of leaving you to find out from a dashboard.

## Every pattern on the infrastructure you already run

Filling the [capability matrix](capabilities.md) is permanent work, not a feature. More backends where a team already runs the service (MySQL and MariaDB, MongoDB, etcd and ZooKeeper), the gaps in existing rows (an outbox for SQLite, a cache on disk or an object store), and patterns that belong in the set at all (a distributed `Semaphore`). A new backend never changes the API you call.

## Background work you can reason about

Work that happens without a request should be as easy to state as work that answers one. Common schedules should read at a glance rather than arrive as five cron fields, and a task should pause without a redeploy. The outbox should state its delivery guarantee in one sentence, which means strict per-key ordering with explicit head-of-line semantics.

## A shorter first hour

Reading the guide should not be the only way to start. A starter template wires one provider, health checks, and one pattern, so a service runs before anything has to be understood. Recipes cover shapes that are not one primitive, such as a multi-step workflow across `Tasks`, `TaskLock`, and the [outbox](outbox/index.md).

## The runtime underneath

grelmicro follows where CPython goes. Free-threaded builds are the near one: state lives in per-process structures that assume one interpreter lock, and that should be fixed before anyone hits it in production. Driving components from more than one event loop is the same class of problem. Hot paths stay measured, with the [benchmarks](benchmarks.md) as the record.

______________________________________________________________________

What is actively being built lives in the [issue tracker](https://github.com/grelinfo/grelmicro/issues). This page is the direction, not the queue.
