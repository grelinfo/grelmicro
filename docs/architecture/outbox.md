# Outbox Internals

This page documents the internal design of the [Outbox](../outbox.md). The user guide covers the API. This page covers the why.

## The dual-write problem

Writing to the database and then "also send an email" as two steps has no atomicity. A crash between them either loses the side effect or runs it for a change that rolled back. The outbox removes the second write to an external system from the critical path: it writes the business row and a message row in one local transaction, and a background relay performs the side effect afterwards. One commit decides both.

This is the transactional outbox pattern. It trades synchronous delivery for atomicity plus eventual delivery.

## Enlisting in the caller's transaction

`publish` never opens or commits a transaction. It runs the message INSERT on the connection or session the caller is already writing on, so the message joins whatever transaction is open there. This is the only shape that composes with an existing unit of work.

- **asyncpg**: the INSERT runs on the passed `Connection`, which is bound to its own transaction. `publish` checks `conn.is_in_transaction()` and refuses a connection in autocommit, because that would commit the message alone.
- **SQLAlchemy and SQLModel**: the INSERT is a Core statement executed on the `AsyncSession`, so it commits with the session's unit of work and never depends on flush ordering. Recognition is an `isinstance` check against SQLAlchemy's async `AsyncSession` and `AsyncConnection`, so a subclass such as SQLModel's `AsyncSession` is accepted while a sync `Session` is refused.
- A **pool or engine** is refused. It hands out a fresh connection, so the message would land in a separate transaction, which is the exact bug the pattern exists to remove.

Owning the transaction instead (a `begin()` that yields a connection) was rejected: it cannot enlist in a transaction the framework does not control, which is the whole point.

## Claiming: SKIP LOCKED plus a lease

The relay claims due messages in one statement:

```sql
UPDATE outbox SET state = 'processing',
    available_at = NOW() + make_interval(secs => $lease),
    attempts = attempts + 1
WHERE id IN (
    SELECT id FROM outbox
    WHERE topic = ANY($topics)
      AND (state = 'pending' OR state = 'processing')
      AND available_at <= NOW()
    ORDER BY available_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT $limit
)
RETURNING ...;
```

Three decisions matter here.

- **`FOR UPDATE SKIP LOCKED`** lets many relays claim disjoint batches with no coordination. The `LIMIT` inside the subquery keeps it from flattening, so the lock is taken once per claimed row. Two relays can never claim the same row.
- **A lease, not the row lock**, gates redelivery. A `FOR UPDATE` row lock lasts only for the claiming transaction, but the handler does external I/O and must not run inside an open transaction. So the claim commits immediately and the `available_at` column carries a visibility deadline. A relay that crashes mid-handler leaves the row invisible only until its lease lapses, then `state = 'processing' AND available_at <= NOW()` re-qualifies it for another relay.
- **`attempts` increments at claim**, not at failure. A crash after claim still counts, and a message whose `attempts` exceed `max_attempts` is dead-lettered at claim before the handler runs again, so a handler that reliably kills its process cannot loop forever.

## Delivery semantics

Delivery is **at least once**. The relay runs the handler, then marks the message done. A crash in between reruns the handler.

Exactly-once *delivery* is impossible: the relay must either mark-then-do (can lose the side effect) or do-then-mark (can repeat it), and no third option exists across a process boundary. The outbox chooses do-then-mark, so the honest guarantee is at-least-once and **handlers must be idempotent**. Every message carries a stable `id` to use as the idempotency key, which composes with the [idempotency](../idempotency.md) primitive.

A stale relay that settles a message another relay has since reclaimed is fenced by `WHERE attempts = $claimed_attempts` on the settle statements, so a late write from a slow relay cannot clobber the reclaiming relay's state. This does not make delivery exactly-once. It only trims duplicate amplification.

## Relay topology

Every replica runs a relay by default. SKIP LOCKED makes that safe at any count, so the outbox scales out with the app and needs no leader election. Leader election would add a dependency, idle replicas, and failover downtime while giving no ordering guarantee (a deposed leader's in-flight handler keeps running during the handover). Run the relay only on some replicas with `relay=False`.

**Polling is the source of truth.** A short poll claims due messages. `LISTEN`/`NOTIFY` is a latency optimization layered on top: `publish` sends a wake inside the caller's transaction, so the relay reacts within milliseconds of a commit. NOTIFY alone is unsafe (a notification is lost on a listener reconnect and does not survive a connection pooler in transaction mode), and a matured `available_at` or a lapsed lease produces no NOTIFY at all, so the poll must remain authoritative. A dropped listener degrades latency to the poll interval, never correctness.

## Ordering

There is no ordering guarantee in this version. Messages are claimed in `(available_at, id)` order but delivered concurrently, and a failed message is rescheduled behind later ones, so even same-`key` messages can be delivered out of order. Ids are time-ordered UUIDv7 so the claim scan and its index stay local, but the id is not a delivery-order promise. Strict per-key ordering is a planned feature with explicit head-of-line-blocking semantics.

## Failure and recovery

- **Handler failure** reschedules with capped exponential backoff and, by default, full jitter (the configurable `retry_jitter` fraction) into `available_at`, or dead-letters after `max_attempts`. `Cancel` and a payload that fails validation dead-letter at once, since retrying cannot help.
- **A crashed relay** strands nothing: the lease lapses and another relay reclaims. A clean shutdown drains in-flight handlers within a grace window, then cancels stragglers, whose leases also lapse.
- **A frozen or unreachable Postgres** surfaces as a bounded `TimeoutError` when the `PostgresProvider` is built with `command_timeout`, so `publish` fails loudly and the business transaction rolls back rather than hanging until the OS TCP timeout. Without it, a claim or publish blocks until the kernel gives up.

## Schema and cleanup

One table holds every message. Column names follow the common outbox convention so change-data-capture tooling can read it. A partial index on `(available_at, id)` for non-terminal rows serves the claim, and a partial unique index on `dedup_key` backs producer-side deduplication. Delivered rows are deleted on success by default, so the working set stays small. Dead rows and (with `keep_delivered=True`) delivered rows are trimmed by `purge`.

## Backend extensibility

The relay talks to an `OutboxBackend` protocol, so a backend is one adapter plus a `provider.outbox()` factory, the same shape [cache](../cache.md) and [coordination](../coordination.md) use. Postgres ships today. SQLite (single-writer, no NOTIFY) is planned, and MySQL (also `FOR UPDATE SKIP LOCKED`) is on the roadmap. The producer and consumer API never changes across backends.
