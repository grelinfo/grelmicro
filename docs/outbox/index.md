# Outbox

The `outbox` module runs an async handler exactly after your database transaction commits, at least once. Use it to turn a side effect into a durable part of your write: send an email, call an external API, or publish an event without ever losing it and without ever running it for a transaction that rolled back.

This is the transactional outbox pattern. It removes the dual-write problem: the moment you write to the database and "also send an email" as two separate steps, a crash between them either loses the email or sends it for a change that never committed. Staging the message in the same transaction makes that impossible.

- **[publish](producer.md)**: stage a message inside your own transaction, so the business row and the message commit together or not at all.
- **[@handler](consumer.md)**: register an async function that the relay runs for each staged message.
- **[relay](relay.md)**: a background worker, started with the app, that delivers staged messages with retries and dead-lettering.

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
    The Postgres backend needs the `postgres` extra: `pip install "grelmicro[postgres]"`. See the [installation guide](../installation.md) for `uv` and `poetry`.

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

The outbox is built backend-first. Adding SQLite or MySQL later is one adapter file plus a `provider.outbox()` factory, the same shape the [cache](../cache/index.md) and [coordination](../coordination/index.md) components already use for their backends. The producer and consumer API never changes when you switch backends.

The Postgres adapter stores messages in a single `grelmicro_outbox` table. The relay claims a batch with `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)`, so every replica claims a disjoint set with no leader and no coordination. The table is created on first connect: pass `auto_migrate=False` when your own migration tool owns the schema, and run the DDL yourself (see [Schema](schema.md)).

## Delivery semantics

Delivery is **at least once**. The relay runs the handler, then marks the message done. A crash in between runs the handler again. Handlers must therefore be idempotent, and every message carries a stable `id` to use as the idempotency key.

To make the side effect itself exactly-once, pass `message.id` as the idempotency key to an external API that accepts one, or wrap the handler with the [idempotency](../idempotency/index.md) primitive keyed on `message.id`. The idempotency store rides the [cache](../cache/index.md) backend, so add a `Cache` component alongside the outbox:

```python
from grelmicro.idempotency import Idempotency, idempotent

charges = Idempotency("charges")


@outbox.handler(ChargeCard)
@idempotent(charges, key=lambda message: message.id)
async def charge(message: Message[ChargeCard]) -> None:
    await payments.charge(message.data)
```

A failed delivery is retried with backoff and eventually dead-lettered. See
[Retries and dead-letter](consumer.md#retries-and-dead-letter).

## Testing

The `MemoryOutboxAdapter` runs the whole outbox in the process with no database. It needs no transaction, so `publish` takes `None` as the handle. Use it in tests and single-process apps:

```python title="testing.py"
--8<-- "outbox/testing.py"
```

Messages live in the process and are lost on restart, and each process keeps its own, so the memory backend does not share an outbox across nodes.
