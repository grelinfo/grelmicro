# Outbox

The `outbox` module runs an async handler exactly after your database transaction commits, at least once. Use it to turn a side effect into a durable part of your write: send an email, call an external API, or publish an event without ever losing it and without ever running it for a transaction that rolled back.

- **[publish](#producer)**: stage a message inside your own transaction, so the business row and the message commit together or not at all.
- **[@handler](#consumer)**: register an async function that the relay runs for each staged message.
- **relay**: a background worker, started with the app, that delivers staged messages with retries and dead-lettering.

This is the transactional outbox pattern. It removes the dual-write problem: the moment you write to the database and "also send an email" as two separate steps, a crash between them either loses the email or sends it for a change that never committed. Staging the message in the same transaction makes that impossible.

## Quick start

Define a payload, stage it inside your transaction, and register a handler. The relay does the rest:

```python
from pydantic import BaseModel, EmailStr

from grelmicro import Grelmicro
from grelmicro.outbox import Message, Outbox
from grelmicro.providers.postgres import PostgresProvider

postgres = PostgresProvider("postgresql://localhost:5432/app")
outbox = Outbox(postgres)

micro = Grelmicro(uses=[outbox])


class WelcomeEmail(BaseModel):
    to: EmailStr
    user_id: int


@outbox.handler(WelcomeEmail)
async def send_welcome(message: Message[WelcomeEmail]) -> None:
    await mailer.send(to=message.data.to, idempotency_key=message.id)


async with postgres.client.acquire() as conn, conn.transaction():
    user_id = await conn.fetchval(
        "INSERT INTO users (email) VALUES ($1) RETURNING id", email
    )
    await outbox.publish(conn, WelcomeEmail(to=email, user_id=user_id))
```

One `COMMIT` makes the user row and the message durable together. `async with micro:` starts the relay, which picks up the message and calls `send_welcome`. If the email API is down, the relay retries with backoff. If it stays down, the message lands in the dead-letter state where you can inspect and redrive it.

## Backend

The outbox is technology-agnostic and delegates storage to a backend. Wire the backend into a `Grelmicro` app via the `Outbox` component. Pass a provider directly to `Outbox(...)`.

!!! tip "Install"
    The Postgres backend needs the `postgres` extra: `pip install "grelmicro[postgres]"`. See the [installation guide](installation.md) for `uv` and `poetry`.

=== "Postgres"
    ```python
    from grelmicro import Grelmicro
    from grelmicro.outbox import Outbox
    from grelmicro.providers.postgres import PostgresProvider

    postgres = PostgresProvider("postgresql://localhost:5432/app")
    micro = Grelmicro(uses=[Outbox(postgres)])
    ```

`async with micro:` opens the provider, creates the table, and starts the relay together.

| | Postgres | SQLite (planned) | MySQL (planned) |
|---|---|---|---|
| **Use case** | Production | Single-host with restart durability | Production (when MySQL is already deployed) |
| **Multi-node relay** | Yes | No (single file) | Yes |
| **Claim** | `FOR UPDATE SKIP LOCKED` | single-writer | `FOR UPDATE SKIP LOCKED` |

The outbox is built backend-first. Adding SQLite or MySQL later is one adapter file plus a `provider.outbox()` factory, the same shape the [cache](cache.md) and [coordination](coordination.md) components already use for their backends. The producer and consumer API never changes when you switch backends.

The Postgres adapter stores messages in a single `grelmicro_outbox` table. The relay claims a batch with `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)`, so every replica claims a disjoint set with no leader and no coordination. The table is created on first connect: pass `auto_migrate=False` when your own migration tool owns the schema.

## Producer

`publish` stages a message. It takes the connection or session you are already writing on, so the message joins your open transaction. It never opens or commits a transaction of its own. That is the whole guarantee: your write and your message share one commit.

### Typed payloads

Pass a Pydantic model. The topic is derived from the model name and the payload is validated at publish time:

```python
class OrderPlaced(BaseModel):
    order_id: int
    total: Decimal


await outbox.publish(conn, OrderPlaced(order_id=42, total=Decimal("9.99")))
```

The handler receives the validated model back as `message.data`:

```python
@outbox.handler(OrderPlaced)
async def on_order(message: Message[OrderPlaced]) -> None:
    await fulfillment.start(message.data.order_id)
```

For a quick call or a message with no model, pass a topic string and a dict instead. The handler reads `message.payload`:

```python
await outbox.publish(conn, "email.welcome", {"to": email})


@outbox.handler("email.welcome")
async def send_welcome(message: Message) -> None:
    await mailer.send(to=message.payload["to"], idempotency_key=message.id)
```

### The transaction rule

`publish` must receive a handle that is already inside an open transaction. This is enforced, because a handle with no transaction would commit the message on its own and quietly break the atomicity that is the point of the pattern:

- An **asyncpg connection** must be inside `conn.transaction()`.
- A **SQLAlchemy `AsyncSession`** must be inside `session.begin()`. The message is inserted immediately into the session's transaction, so it commits with the unit of work and never depends on flush ordering. SQLModel's `AsyncSession` is a subclass, so it works the same way.
- Passing a **pool or an engine** raises. A pool hands out a different connection, so the message would land in a separate transaction.

### SQLModel and SQLAlchemy

Add the message to the same `session.begin()` block as your ORM writes. They commit together:

```python
from sqlmodel import Field, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession


class Hero(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str


async def create_hero(session: AsyncSession, name: str) -> None:
    async with session.begin():
        session.add(Hero(name=name))
        await outbox.publish(session, "hero.created", {"name": name})
```

The Hero row and the `hero.created` message share one commit. A rollback drops both. This is identical with a plain SQLAlchemy `AsyncSession`, since SQLModel's session subclasses it.

### FastAPI

Use a per-request session or connection dependency and hand it to `publish`. The message joins the request's transaction with no extra wiring:

=== "SQLModel"
    ```python
    from sqlmodel.ext.asyncio.session import AsyncSession


    async def get_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine) as session, session.begin():
            yield session


    @app.post("/signup")
    async def signup(
        body: SignUp, session: AsyncSession = Depends(get_session)
    ) -> None:
        session.add(User(email=body.email))
        await outbox.publish(session, WelcomeEmail(to=body.email))
    ```

=== "asyncpg"
    ```python
    async def get_conn() -> AsyncIterator[asyncpg.Connection]:
        async with postgres.client.acquire() as conn, conn.transaction():
            yield conn


    @app.post("/signup")
    async def signup(
        body: SignUp, conn: asyncpg.Connection = Depends(get_conn)
    ) -> None:
        user_id = await conn.fetchval(
            "INSERT INTO users (email) VALUES ($1) RETURNING id", body.email
        )
        await outbox.publish(conn, WelcomeEmail(to=body.email, user_id=user_id))
    ```

The session dependency opens one transaction per request, so the `User` row and the `WelcomeEmail` message commit together or roll back together.

### Delay and deduplication

`delay` holds a message back until a future time. `dedup_key` drops a duplicate before it is stored, using an insert that does nothing on conflict, so a producer retry is safe and never raises:

```python
await outbox.publish(conn, ReminderDue(...), delay=timedelta(hours=1))

await outbox.publish(conn, OrderPlaced(...), dedup_key=f"order:{order_id}")
```

When delivered messages are deleted (the default), the deduplication window lasts only until delivery. Keep delivered messages with `keep_delivered=True` to extend it.

## Consumer

A handler is an async function bound to a payload model or a topic string. Register as many as you like:

```python
@outbox.handler(WelcomeEmail)
async def send_welcome(message: Message[WelcomeEmail]) -> None:
    await mailer.send(to=message.data.to, idempotency_key=message.id)
```

`Message` carries everything the relay knows:

| field | meaning |
|---|---|
| `id` | stable message id, use it as the idempotency key for the side effect |
| `topic` | routing topic |
| `key` | ordering or partition key, `None` by default |
| `data` | the validated payload model, for typed handlers |
| `payload` | the raw payload dict, for topic handlers |
| `headers` | metadata and trace context |
| `attempts` | how many times delivery has been tried, starting at 1 |

### Controlling retries from the handler

Any exception retries the message with backoff. Raise `Retry` to reschedule on your own terms, or `Cancel` to dead-letter it now without burning the remaining attempts:

```python
from grelmicro.outbox import Cancel, Retry


@outbox.handler(ChargeCard)
async def charge(message: Message[ChargeCard]) -> None:
    result = await payments.charge(message.data, idempotency_key=message.id)
    if result.rate_limited:
        raise Retry(delay=timedelta(seconds=30))
    if result.card_declined:
        raise Cancel(reason="card declined")
```

## Relay

The relay is a background worker started with `async with micro:`. It is resource-efficient and asyncio-native.

- **Every replica runs a relay.** They claim disjoint batches with `FOR UPDATE SKIP LOCKED`, so the outbox scales out with your app and needs no leader election.
- **A relay claims only topics it has a handler for.** During a rolling deploy an old replica leaves a new topic alone instead of dead-lettering it, and the message waits for a replica that knows it. A message whose topic has no handler anywhere stays pending until one is registered.
- **A single dedicated connection listens for `NOTIFY`.** `publish` sends a wake inside your transaction, so the relay reacts within milliseconds of a commit. Polling stays the source of truth on a short interval, because a notification is lost if the listener reconnects and does not survive a connection pooler in transaction mode.
- **Handlers run outside any database transaction.** The relay claims a batch, commits, releases the connection, then runs the handlers. No connection is held across a handler's network call.
- **A lease makes a crash self-healing.** A claimed message is invisible until its lease expires. If a relay dies mid-handler, the lease lapses and another relay reclaims the message. Keep handlers under `lease_duration` (30 seconds by default).
- **Concurrency is bounded.** The relay claims only as many messages as it has free handler slots, so a lease covers real work and never expires while a message waits in a local queue.

!!! warning "Connection poolers"
    `LISTEN`/`NOTIFY` does not work through PgBouncer in transaction pooling mode. Set `notify=False` there and rely on polling. Lower `poll_interval` if you want tighter latency.

!!! tip "Bounded failure"
    Build the `PostgresProvider` with `command_timeout` so a frozen or unreachable Postgres surfaces as a `TimeoutError` in bounded time. `publish` then fails loudly and your business transaction rolls back, instead of hanging until the OS TCP timeout. See [Providers](providers.md).

For the design rationale behind the claim protocol, the lease, and the delivery guarantees, see [Outbox Internals](architecture/outbox.md).

### Scaling the relay

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

### Ordering

The outbox does not guarantee ordering in this version. Messages are delivered at least once and concurrently, so two messages, even with the same `key`, may be delivered out of order, most visibly when the first one is retried after a failure. Design handlers to tolerate reordering. Strict per-key ordering is a planned feature with explicit head-of-line semantics.

## Delivery semantics

Delivery is **at least once**. The relay runs the handler, then marks the message done. A crash in between runs the handler again. Handlers must therefore be idempotent, and every message carries a stable `id` to use as the idempotency key.

To make the side effect itself exactly-once, pass `message.id` as the idempotency key to an external API that accepts one, or wrap the handler with the [idempotency](idempotency.md) primitive keyed on `message.id`. The idempotency store rides the [cache](cache.md) backend, so add a `Cache` component alongside the outbox:

```python
from grelmicro.idempotency import Idempotency, idempotent

charges = Idempotency("charges")


@outbox.handler(ChargeCard)
@idempotent(charges, key=lambda message: message.id)
async def charge(message: Message[ChargeCard]) -> None:
    await payments.charge(message.data)
```

### Retries and dead-letter

A failed delivery is retried with capped exponential backoff and full jitter. After `max_attempts` the message moves to the `dead` state with its last error recorded. It stops blocking the queue and is left for you to inspect. Redrive dead messages back to pending once the cause is fixed:

```python
await outbox.redrive(topic="email.welcome")
```

Alert on the oldest pending age and on any message entering the dead state.

### Cleanup

Delivered rows are deleted on success by default, so the table stays small on its own. Dead rows, and delivered rows kept with `keep_delivered=True`, accumulate until you trim them. Use `purge` to delete terminal rows, optionally only those past a retention window:

```python
from datetime import timedelta

await outbox.purge()                              # all delivered and dead rows
await outbox.purge(older_than=timedelta(days=7))  # only those older than 7 days
```

Pending and in-flight messages are never touched. Run it from a scheduled [task](task.md) for hands-off retention.

## Observability

With the [trace](tracing.md) component configured, `publish` writes the current trace context into the message `headers`, and the relay opens a consumer span for each delivery parented on it. A request that stages a message links to the delivery that runs later, even across replicas. The span follows the messaging semantic conventions (`messaging.system`, `messaging.destination.name`, `messaging.operation`, `messaging.message.id`).

With the [metrics](metrics.md) component configured, the relay emits:

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

## Testing

The `MemoryOutboxAdapter` runs the whole outbox in the process with no database. It needs no transaction, so `publish` takes `None` as the handle. Use it in tests and single-process apps:

```python title="testing.py"
--8<-- "outbox/testing.py"
```

Messages live in the process and are lost on restart, and each process keeps its own, so the memory backend does not share an outbox across nodes.

## Schema

The Postgres adapter uses one table. Column names follow the common outbox convention, so change-data-capture tooling can read it directly:

| column | type | purpose |
|---|---|---|
| `id` | `uuid` primary key | stable message id and idempotency key, time-ordered UUIDv7 |
| `topic` | `text` | routes to the handler |
| `key` | `text` null | ordering or partition key |
| `payload` | `jsonb` | the message body |
| `headers` | `jsonb` | metadata and trace context |
| `dedup_key` | `text` null | producer-side deduplication |
| `attempts` | `int` | delivery attempt counter |
| `available_at` | `timestamptz` | when the message is next actionable, for delay, retry, and lease |
| `state` | `text` | `pending`, `processing`, `delivered`, or `dead` |
| `last_error` | `text` null | the last handler error, for dead messages |
| `created_at` | `timestamptz` | staged time |

Ids are time-ordered UUIDv7, so the claim orders by `(available_at, id)` for stable, index-friendly delivery. A partial index on `available_at` for non-terminal rows serves the claim query, and a unique partial index on `dedup_key` (where it is set) backs deduplication. The table is created on first connect unless `auto_migrate=False`, guarded so replicas booting together do not race the DDL.

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
| `keep_delivered` | `False` | keep delivered rows instead of deleting them |
| `auto_migrate` | `True` | create the table on first connect |
| `notify` | `True` | use `LISTEN`/`NOTIFY` for low-latency wakeups |
