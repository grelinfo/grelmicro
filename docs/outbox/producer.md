# Producer

`publish` stages a message. It takes the connection or session you are already writing on, so the message joins your open transaction. It never opens or commits a transaction of its own. That is the whole guarantee: your write and your message share one commit.

## Typed payloads

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

## The transaction rule

`publish` must receive a handle that is already inside an open transaction. This is enforced, because a handle with no transaction would commit the message on its own and quietly break the atomicity that is the point of the pattern:

- An **asyncpg connection** must be inside `conn.transaction()`.
- A **SQLAlchemy `AsyncSession`** must be inside `session.begin()`. The message is inserted immediately into the session's transaction, so it commits with the unit of work and never depends on flush ordering. SQLModel's `AsyncSession` is a subclass, so it works the same way.
- Passing a **pool or an engine** raises. A pool hands out a different connection, so the message would land in a separate transaction.

## SQLModel and SQLAlchemy

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

## FastAPI

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

## Resolving the outbox

A producer that only publishes does not need to hold the constructed instance or a config-bound module singleton. `Outbox.current()` returns the app-registered outbox, so a request handler in another module publishes without importing it:

```python
from grelmicro.outbox import Outbox


@app.post("/signup")
async def signup(body: SignUp, session: AsyncSession = Depends(get_session)) -> None:
    session.add(User(email=body.email))
    await Outbox.current().publish(session, WelcomeEmail(to=body.email))
```

`Outbox.current(name=...)` selects a named instance. It resolves inside `async with micro:` or after `micro.install(app)`, and raises `OutOfContextError` otherwise. Handlers still register on the instance at wiring time.

## Delay and deduplication

`delay` holds a message back until a future time. `dedup_key` drops a duplicate before it is stored, using an insert that does nothing on conflict, so a producer retry is safe and never raises:

```python
await outbox.publish(conn, ReminderDue(...), delay=timedelta(hours=1))

await outbox.publish(conn, OrderPlaced(...), dedup_key=f"order:{order_id}")
```

When delivered messages are deleted (the default), the deduplication window lasts only until delivery. Keep delivered messages with `keep_delivered=True` to extend it, or `keep_delivered=timedelta(days=30)` to extend it for a fixed window. A retention window caps it: once a delivered row is purged its `dedup_key` frees up. See [Retention and cleanup](relay.md#retention-and-cleanup).

!!! tip "Bounded failure"
    Build the `PostgresProvider` with `command_timeout` so a frozen or unreachable Postgres surfaces as a `TimeoutError` in bounded time. `publish` then fails loudly and your business transaction rolls back, instead of hanging until the OS TCP timeout. See [Providers](../providers/postgres.md).
