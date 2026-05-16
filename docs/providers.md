# Providers

A **Provider** is a first-class connection object. It owns the vendor URL,
the native client (a Redis pool, an asyncpg pool, ...), and the lifecycle
of both. Components like `Sync`, `Cache`, and `RateLimit` accept a
Provider directly and use its canonical adapter under the hood.

Two providers ship today: `RedisProvider` and `PostgresProvider`. More
will follow.

## The canonical shape

Pass a Provider to every Component that needs the same connection:

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimit
from grelmicro.sync import Sync

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(uses=[
    redis,
    Sync(redis),
    Cache(redis),
    RateLimit(redis),
])

async with micro:
    ...
```

Components dispatch to the Provider's factory methods (`provider.sync()`,
`provider.cache()`, `provider.ratelimiter()`). The Adapter classes
(`RedisSyncAdapter`, `RedisCacheAdapter`, `RedisRateLimiterAdapter`) stay
public as escape hatches but rarely appear in user code.

## Recipe 1: env-driven

Construct the Provider without arguments and let it read `REDIS_*` from
the environment:

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.providers.redis import RedisProvider
from grelmicro.sync import Sync

redis = RedisProvider()  # reads REDIS_URL or REDIS_HOST + REDIS_PORT + ...

micro = Grelmicro(uses=[
    redis,
    Sync(redis),
    Cache(redis),
])
```

Set `REDIS_URL` (or `REDIS_HOST` + `REDIS_PORT` + `REDIS_DB` +
`REDIS_PASSWORD`) in the environment.

## Recipe 2: split pools by env prefix

Two Redis instances (or two databases) live behind different prefixes.
Each prefix gets its own Provider:

```python
cache_redis = RedisProvider(env_prefix="CACHE_REDIS_")
session_redis = RedisProvider(env_prefix="SESSION_REDIS_")

micro = Grelmicro(uses=[
    cache_redis,
    session_redis,
    Sync(session_redis),
    Cache(cache_redis),
])
```

Set `CACHE_REDIS_URL` and `SESSION_REDIS_URL` (or the decomposed forms).
The two components talk to two pools.

## Recipe 3: bring your own client

You already own a Redis client (custom retry, sentinel, auth, or a
testcontainers fixture). Wrap it with `from_client`:

```python
import redis.asyncio as redis

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.providers.redis import RedisProvider

client = redis.Redis(host="prod.cache", socket_timeout=5)
redis_provider = RedisProvider.from_client(client)  # caller owns the client

micro = Grelmicro(uses=[redis_provider, Cache(redis_provider)])
```

Pass `own=True` to hand ownership to the provider. It will close the
client when the provider exits, useful in pytest fixtures:

```python
@pytest.fixture
async def redis_provider(redis_container):
    async with RedisProvider.from_client(
        redis_container.get_client(), own=True
    ) as provider:
        yield provider
```

## Construction forms

```python
RedisProvider("redis://localhost:6379")      # positional URL
RedisProvider(url="redis://...")             # keyword URL
RedisProvider(host="x", port=6379, db=0)     # decomposed kwargs
RedisProvider()                              # env-driven (REDIS_*)
RedisProvider(env_prefix="CACHE_REDIS_")     # custom env prefix
RedisProvider(env_load=False)                # kwargs only, no env
RedisProvider.from_config(RedisConfig(...))  # from a config object
RedisProvider.from_client(client)            # bring-your-own client
```

## Factory methods

Each Provider exposes factory methods that return its canonical adapter:

| Method                      | Returns                       | RedisProvider | PostgresProvider |
|----------------------------|-------------------------------|:-------------:|:----------------:|
| `.sync(**kwargs)`           | `SyncBackend` implementation  |       ✓        |        ✓         |
| `.cache(**kwargs)`          | `CacheBackend` implementation |       ✓        |        —         |
| `.ratelimiter(**kwargs)`    | `RateLimiterBackend` impl     |       ✓        |        —         |
| `.breaker(**kwargs)`        | `CircuitBreakerBackend` impl  |       —        |        —         |

Factories that do not apply raise `NotImplementedError` with a message
pointing to the right alternative. `Sync(provider)`, `Cache(provider)`,
`RateLimit(provider)`, and `Breaker(provider)` call these factories.

## Postgres

`PostgresProvider` ships the `.sync()` factory. The provider wraps an
`asyncpg.Pool` and opens it lazily on `__aenter__`.

```python
from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.sync import Sync

postgres = PostgresProvider("postgresql://localhost/app")

micro = Grelmicro(uses=[
    postgres,
    Sync(postgres),
])
```

Set `POSTGRES_URL` (or `POSTGRES_HOST` + `POSTGRES_PORT` + `POSTGRES_DB`
+ `POSTGRES_USER` + `POSTGRES_PASSWORD`) for env-driven construction.

For two pools (writer + reader), split by env prefix:

```python
write = PostgresProvider(env_prefix="WRITE_POSTGRES_")
read = PostgresProvider(env_prefix="READ_POSTGRES_")

micro = Grelmicro(uses=[
    write,
    read,
    Sync(write),
    Sync(read, name="read"),
])
```

Construction forms:

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

## Lifecycle

The Provider is opened when the `Grelmicro` app enters and closed when
the app exits. Components borrow the Provider's client without managing
its lifecycle.

Always **list the Provider before** the Components that depend on it.
`uses=` opens items in declaration order. `PostgresProvider` builds its
`asyncpg.Pool` on `__aenter__`, so a Component placed before its
Provider would access `provider.client` before the pool exists and raise
`OutOfContextError`. `Grelmicro.__aenter__` warns on this ordering, but
the correct fix is to list the Provider first.

## Memory backends

In-memory backends (`MemorySyncAdapter`, `MemoryCacheAdapter`,
`MemoryRateLimiterAdapter`, `MemoryCircuitBreakerAdapter`) have no
provider. Pass the adapter directly to its Component:

```python
from grelmicro import Grelmicro
from grelmicro.resilience import Breaker
from grelmicro.resilience.memory import MemoryCircuitBreakerAdapter

micro = Grelmicro(uses=[
    Breaker(MemoryCircuitBreakerAdapter()),
])
```
