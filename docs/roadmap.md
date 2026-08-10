# Roadmap

Where grelmicro is going. Every direction here is additive, and no dates are promised.

This page describes direction, not a queue. It names a few concrete things under each heading so the direction is not vague, but the list is an illustration rather than a commitment. For what ships today, read the [capability matrix](capabilities.md).

## The HTTP edge

A pattern you already use should reach the wire without hand-written glue. Today a rate limiter hands you a `RateLimitResult` and you build the 429 yourself, and an error becomes a response only if you map it.

The direction is to close that last gap wherever HTTP has semantics that match a pattern: rate limit response headers rendered for you, problem-detail error responses, admission applied per route rather than per handler, and dependency helpers that read like the framework's own. The pure-ASGI middleware already runs on Starlette and Litestar, so the same reach extends past FastAPI.

## Resilience that composes and coordinates

The primitives exist. What is missing is making them work together, and making them work across replicas.

Composition first. The docs give the correct outside-in order and then leave you to stack the decorators by hand, where a wrong order silently changes the semantics. One entry point should apply them in the right order whatever order you list them, and a runtime `compose()` should build a policy list when the shape is not known at import.

Coordination second. Every retry budget, bulkhead, and adaptive limiter is per-process today, so ten replicas retry ten times as hard as one. The direction is to share that state on the backends you already run, the way the rate limiter and circuit breaker already do.

Tail latency is the third piece. A deadline should propagate through `Timeout`, `Retry`, and `Shield` instead of being re-stated at each layer, and a call that is merely slow should have an answer of its own, such as firing a backup attempt and taking whichever returns first.

## Observability that closes the loop

Every component emits metrics. The next step is depth, and reacting rather than only watching.

Depth means the numbers an operator actually pages on: how long a lock took to acquire, how far a scheduled task drifted from its planned start, why a run was skipped, how full a connection pool is, and exemplars linking a metric back to the trace that produced it. Reacting means a circuit breaker that calls your code when it trips, instead of leaving you to discover it in a dashboard.

## Every pattern on the infrastructure you already run

The [capability matrix](capabilities.md) is the honest version of this. Filling it is a permanent direction rather than a feature.

That means more backends where a team already runs the service (MySQL and MariaDB, MongoDB, etcd and ZooKeeper), the gaps in existing rows (an outbox for SQLite, a cache on local disk or an object store), and new patterns that belong in the set at all (a distributed `Semaphore` alongside `Lock` and `ReadWriteLock`). A new backend never changes the API you call.

## Background work you can reason about

Work that happens without a request should be as easy to state as work that answers one.

For schedules, that means saying the common ones plainly, so `daily` and `hourly` read at a glance instead of arriving as a five-field expression, and being able to pause a task without redeploying. For the outbox, it means delivery guarantees you can state in one sentence. Ordering is the open one: messages are delivered at least once and concurrently today, and strict per-key ordering with explicit head-of-line semantics is the direction.

## A shorter first hour

Reading the guide should not be the only way to start.

A starter template that wires one provider, health checks, and one pattern gets a service running before anything has to be understood. Recipes cover the shapes that are not one primitive, such as orchestrating a multi-step workflow across `Tasks`, `TaskLock`, and the [outbox](outbox.md). Both stay docs-first, and become an API only if the same code keeps getting copied.

## The runtime underneath

grelmicro targets asyncio on CPython and intends to keep up with where CPython is going.

Free-threaded builds are the near one. The library holds state in per-process structures that assume one interpreter lock, and running correctly without it is work worth doing before anyone hits it in production. Driving components from more than one event loop in a single process is the same class of problem. Hot paths stay under measurement, with the [benchmarks](benchmarks.md) as the record.

______________________________________________________________________

What is actively being built lives in the [issue tracker](https://github.com/grelinfo/grelmicro/issues). This page is the direction, not the queue.
