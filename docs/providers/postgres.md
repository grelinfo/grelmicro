# Postgres

`PostgresProvider` ships all factory methods: `.lock()`, `.leaderelection()`, `.cache()`, `.outbox()`, `.ratelimiter()`, `.circuitbreaker()`, and `.schedule()`. The
provider wraps an `asyncpg.Pool` and opens it lazily on `__aenter__`.

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.providers.postgres import PostgresProvider

postgres = PostgresProvider("postgresql://localhost/app")

micro = Grelmicro(uses=[
    Coordination(postgres),
])
```

Install the `postgres` extra first: `pip install "grelmicro[postgres]"`.

A SQLAlchemy-style URL works as it is. The provider drops the driver
suffix, so an app already holding `postgresql+asyncpg://localhost/app`
passes it straight through with no string surgery:

```python
postgres = PostgresProvider("postgresql+asyncpg://localhost/app")
```

The suffix names the client library that app uses, not the wire
protocol, and this provider always connects with asyncpg.

## Environment variables

Set `POSTGRES_URL` (or `POSTGRES_HOST` + `POSTGRES_PORT` + `POSTGRES_DB`
+ `POSTGRES_USER` + `POSTGRES_PASSWORD`) for env-driven construction. The
database name also reads from `POSTGRES_DATABASE` when `POSTGRES_DB` is
unset, so both the `postgres` Docker image convention and the longer
spelling work.

## Bounding a hung server

Pass `command_timeout` (or set `POSTGRES_COMMAND_TIMEOUT`) to bound every operation. A query that hangs on a frozen or unreachable server then raises `TimeoutError` after that many seconds, instead of blocking until the OS TCP timeout. It defaults to `None` (no timeout).

```python
postgres = PostgresProvider("postgresql://localhost/app", command_timeout=5)
```

This matters most for the [outbox](../outbox/producer.md), where `publish` runs
inside your business transaction.

## Share a SQLAlchemy engine

An app that already has a SQLAlchemy engine hands it over instead of
opening a second pool:

```python
from sqlalchemy.ext.asyncio import create_async_engine

from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider

engine = create_async_engine("postgresql+asyncpg://localhost/app")

postgres = PostgresProvider.from_engine(engine)

micro = Grelmicro(uses=[postgres])
```

Every operation borrows a connection from the engine's pool and gives it
back. The database sees one pool, sized by the settings the app already
chose, and the app keeps ownership: the provider does not dispose an
engine it was handed. Pass `own=True` to hand that over, and the provider
disposes the engine when it exits.

Size the engine for two connections per request, not one. A request that
already holds a connection, inside `async with session.begin()`, needs a
second one the moment it takes a lock or reads the cache. With
`pool_size=10` and no overflow, ten such requests at once each hold one
connection and wait for another, and every one of them blocks for
`pool_timeout` before failing. Raise `pool_size`, or leave `max_overflow`
room, so grelmicro is never queued behind the request that is calling it.

Budget one more for the [outbox](../outbox/index.md) listener, which holds
a connection for as long as the app runs. A lock, a cache, and a rate
limiter each borrow one only for the length of a call.

The engine has to use the `postgresql+asyncpg` dialect, because grelmicro
runs asyncpg statements. Another driver is refused at construction:

```python
engine = create_async_engine("postgresql+psycopg://localhost/app")

PostgresProvider.from_engine(engine)
# SettingsValidationError: Could not validate settings:
# engine: driver should be 'asyncpg', got 'psycopg'
```

Pass the URL instead to open a separate asyncpg pool alongside that
engine:

```python
postgres = PostgresProvider(engine.url.render_as_string(hide_password=False))
```

Two things the engine decides for you.

**The timeout is the engine's.** `command_timeout` bounds a hung server on a
provider that opens its own pool. A borrowed engine connects on its own terms,
so pass the timeout there instead:

```python
engine = create_async_engine(
    "postgresql+asyncpg://localhost/app",
    connect_args={"command_timeout": 5},
)
```

Without it, a statement grelmicro runs waits as long as the engine's
connections do.

**The schema is the engine's too.** grelmicro names its tables unqualified, so
they resolve through whatever `search_path` the connection carries. That is
what lets an app keep its own `search_path` across the loan. An app that
switches `search_path` per checkout, one schema per tenant, gives each tenant
its own copy of the lock table, and a lock meant to be shared stops being
shared. Qualify the table names, or give grelmicro its own engine, if the
schema moves per request.

!!! warning "Pass the engine, never a live session"
    `from_engine` takes an `AsyncEngine`. An `AsyncSession` or an
    `AsyncConnection` is refused, because grelmicro would then write
    inside whatever transaction the caller has open. A lock released in a
    request that later rolls back would come back locked.

    `outbox.publish()` is the one call that takes your session, and it
    takes it on purpose. See [Producing](../outbox/producer.md).

## Two pools

For a writer and a reader, split by env prefix:

```python
write = PostgresProvider(env_prefix="WRITE_POSTGRES_")
read = PostgresProvider(env_prefix="READ_POSTGRES_")

micro = Grelmicro(uses=[
    write,
    read,
    Coordination(write),
    Coordination(read, name="read"),
])
```

## Construction forms

```python
PostgresProvider("postgresql://localhost/app")  # positional URL
PostgresProvider(url="postgresql://...")        # keyword URL
PostgresProvider(host="db", port=5432, database="app", user="u", password="pw")
PostgresProvider()                              # env-driven (POSTGRES_*)
PostgresProvider(env_prefix="WRITE_POSTGRES_")  # custom env prefix
PostgresProvider(env_load=False)                # kwargs only, no env
PostgresProvider.from_config(PostgresConfig(...))
PostgresProvider.from_client(pool)              # bring-your-own pool
PostgresProvider.from_engine(engine)            # share a SQLAlchemy engine
```
