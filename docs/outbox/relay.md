# Relay

The relay is a background worker started with `async with micro:`. It is resource-efficient and asyncio-native.

- **Every replica runs a relay.** They claim disjoint batches with `FOR UPDATE SKIP LOCKED`, so the outbox scales out with your app and needs no leader election.
- **A relay claims only topics it has a handler for.** During a rolling deploy an old replica leaves a new topic alone instead of dead-lettering it, and the message waits for a replica that knows it. A message whose topic has no handler anywhere stays pending until one is registered.
- **A single dedicated connection listens for `NOTIFY`.** `publish` sends a wake inside your transaction, so the relay reacts within milliseconds of a commit. Polling stays the source of truth on a short interval, because a notification is lost if the listener reconnects and does not survive a connection pooler in transaction mode.
- **Handlers run outside any database transaction.** The relay claims a batch, commits, releases the connection, then runs the handlers. No connection is held across a handler's network call.
- **A lease makes a crash self-healing.** A claimed message is invisible until its lease expires. If a relay dies mid-handler, the lease lapses and another relay reclaims the message. Keep handlers under `lease_duration` (30 seconds by default).
- **Concurrency is bounded.** The relay claims only as many messages as it has free handler slots, so a lease covers real work and never expires while a message waits in a local queue.

!!! warning "Connection poolers"
    `LISTEN`/`NOTIFY` does not work through PgBouncer in transaction pooling mode. Set `notify=False` there and rely on polling. Lower `poll_interval` if you want tighter latency.

For the design rationale behind the claim protocol, the lease, and the delivery guarantees, see [Outbox Internals](../architecture/outbox.md).

## Scaling the relay

Every replica runs a relay by default, and `FOR UPDATE SKIP LOCKED` keeps that safe at any number: relays claim disjoint messages, never the same one, so the count only affects resources, never correctness.

The cost of each relay is one dedicated `LISTEN` connection plus a share of the wake-ups. At a high replica count those connections add up against `max_connections`, and many relays waking on the same `NOTIFY` race to claim and mostly find nothing.

Run the relay only where you want it with `relay=False`. The common shape is many web replicas that publish and a small worker deployment that relays:

```python
# web pods: publish only, no relay
micro = Grelmicro(uses=[Outbox(postgres, relay=False)])

# worker pods: run the relay
micro = Grelmicro(uses=[Outbox(postgres, relay=True)])
```

`relay=True` is the default, so a single deployment works out of the box. `concurrency` bounds the handlers running at once inside each relay.

## Ordering

The outbox does not guarantee ordering in this version. Messages are delivered at least once and concurrently, so two messages, even with the same `key`, may be delivered out of order, most visibly when the first one is retried after a failure. Design handlers to tolerate reordering. Strict per-key ordering, with explicit head-of-line semantics, is [on the roadmap](../roadmap.md).

## Retention and cleanup

Delivered rows are deleted on success by default (`keep_delivered=False`), so the table stays small on its own.

Set `keep_delivered` to a `timedelta` to keep delivered rows for a window and let the relay purge them once they age out:

```python
from datetime import timedelta

micro = Grelmicro(uses=[Outbox(postgres, keep_delivered=timedelta(days=30))])
```

The window is measured from delivery time, not publish time, so a delayed or heavily retried message still gets its full retention. The relay purges expired delivered rows in the background, so retention needs no scheduled job. The purge runs on the relay, so a `relay=False` replica never purges. Set `keep_delivered=True` to keep delivered rows for good with no auto-purge.

Auto-purge only removes delivered rows. A dead-letter is a failure to inspect and redrive, so dead rows are never deleted automatically. Trim them yourself with `purge`, which deletes both delivered and dead rows, optionally only those past a window:

```python
from datetime import timedelta

await outbox.purge()                              # all delivered and dead rows
await outbox.purge(older_than=timedelta(days=7))  # only those older than 7 days
```

### Retention is your decision, not a default

A message payload sits in your database until the row is deleted. Whatever you
publish is stored, so a payload carrying a password reset token, a signed URL, or
a one-time code is at rest in the outbox table for as long as the row lives.

The default deletes a delivered row immediately, which is the safe end of the
range. Do not rely on that. Pin the value you want:

```python
micro = Grelmicro(uses=[Outbox(postgres, keep_delivered=False)])
```

Pinning it says the choice was made, and a later release cannot move it under
you. Two things are worth separating when you choose:

- **Delivered rows** follow `keep_delivered`. A window keeps them for
  replay and audit, and the relay purges them once they age out.
- **Dead rows are never purged automatically**, whatever `keep_delivered`
  says, because a dead-letter exists to be inspected. A payload that failed
  to deliver therefore stays until you call `purge`. That is the case people
  miss.

Keep single-use secrets out of the payload where you can. Publish a reference
and let the consumer fetch the secret, so the outbox holds an identifier rather
than the credential itself.

Pending and in-flight messages are never touched. Run `purge` from a scheduled [task](../task.md) when you keep delivered rows for good and still want dead rows trimmed.

## Observability

With the [trace](../tracing.md) component configured, `publish` writes the current trace context into the message `headers`, and the relay opens a consumer span for each delivery parented on it. A request that stages a message links to the delivery that runs later, even across replicas. The span follows the messaging semantic conventions (`messaging.system`, `messaging.destination.name`, `messaging.operation`, `messaging.message.id`).

With the [metrics](../metrics.md) component configured, the relay emits:

| Metric | Type | Meaning |
|---|---|---|
| `grelmicro.outbox.published` | counter | messages staged by `publish` |
| `grelmicro.outbox.delivered` | counter | successful deliveries |
| `grelmicro.outbox.retried` | counter | deliveries rescheduled after a failure |
| `grelmicro.outbox.dead_lettered` | counter | messages moved to the dead state |
| `grelmicro.outbox.handler_duration` | histogram | handler run time in seconds |

Each carries a `topic` attribute. Both integrations are no-ops when the components are absent, so there is no cost when you do not use them.

The relay also logs each retry at warning level and each dead-letter at error level, with the message id, topic, attempt count, and last error. Alert on the dead-lettered count and on any message entering the dead state.

You can also set your own `headers` on `publish` and read them in the handler for correlation ids or routing metadata.

## Configuration

`OutboxConfig` is a plain Pydantic model. Component defaults read from the environment under `GREL_OUTBOX_` unless you set fields directly.

| field | default | description |
|---|---|---|
| `table` | `grelmicro_outbox` | table name |
| `relay` | `True` | run the background relay on this replica |
| `poll_interval` | `1.5` | seconds between fallback polls |
| `batch_size` | `100` | claim ceiling per cycle, capped by free handler slots |
| `lease_duration` | `30` | seconds a claimed message stays invisible |
| `max_attempts` | `10` | attempts before dead-lettering |
| `retry_base` | `1` | base backoff in seconds |
| `retry_max` | `300` | maximum backoff in seconds |
| `retry_jitter` | `1` | jitter fraction applied to backoff |
| `concurrency` | `50` | maximum handlers running at once |
| `dead_letter` | `True` | move exhausted messages to the dead state |
| `keep_delivered` | `False` | keep delivered rows instead of deleting them, or a `timedelta` to keep and auto-purge them after that window |
| `auto_migrate` | `True` | create the table on first connect |
| `notify` | `True` | use `LISTEN`/`NOTIFY` for low-latency wakeups |
