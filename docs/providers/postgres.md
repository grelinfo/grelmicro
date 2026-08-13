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
```
