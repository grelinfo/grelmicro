# Providers

A **Provider** is a first-class connection object. It owns the vendor URL,
the native client (a Redis pool, an asyncpg pool, ...), and the lifecycle
of both. Components like `Coordination`, `Cache`, and `RateLimiterRegistry` accept a
Provider directly and use its matching adapter under the hood.

Five providers ship today: `RedisProvider`, `ValkeyProvider`, `PostgresProvider`,
`SQLiteProvider`, and `MemoryProvider`. More will follow.

## Recommended shape

Pass a Provider to every Component that needs the same connection:

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.coordination import Coordination
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimiterRegistry

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(uses=[
    Coordination(redis),
    Cache(redis),
    RateLimiterRegistry(redis),
])

async with micro:
    ...
```

Components dispatch to the Provider's factory methods (`provider.lock()`,
`provider.cache()`, `provider.ratelimiter()`). The Adapter classes
(`RedisLockAdapter`, `RedisCacheAdapter`, `RedisRateLimiterAdapter`) stay
public as escape hatches but rarely appear in user code.

!!! tip "Listing the Provider is optional"
    A Provider held by a Component is discovered and lifecycled for you, so
    you can drop the top-level entry and let the Components carry it:

    ```python
    micro = Grelmicro(uses=[
        Coordination(redis),
        Cache(redis),
        RateLimiterRegistry(redis),
    ])
    ```

    The shared `redis` opens once, before the Components that hold it.
    Listing it explicitly is still valid and lets you control where it sits
    in the lifecycle order.

## Recipe 1: env-driven

Construct the Provider without arguments and let it read `REDIS_*` from
the environment:

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.coordination import Coordination
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider()  # reads REDIS_URL or REDIS_HOST + REDIS_PORT + ...

micro = Grelmicro(uses=[
    Coordination(redis),
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
    Coordination(session_redis),
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

micro = Grelmicro(uses=[Cache(redis_provider)])
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

## Sentinel and Cluster

`RedisProvider` switches topology from the URL scheme. The scheme rides
the same `url` field, so `REDIS_URL` alone selects standalone, Sentinel,
or Cluster with no other code change. `ValkeyProvider` reads the same
schemes and builds the Valkey equivalents.

Standalone stays as before:

```python
RedisProvider("redis://localhost:6379/0")
```

Sentinel lists the Sentinel hosts in the authority. The first path
segment is the master service name. An optional second segment is the
database index.

```python
RedisProvider("redis+sentinel://host1:26379,host2:26379/mymaster/0")
```

Cluster lists the seed nodes. The client discovers the rest of the
topology from them.

```python
RedisProvider("redis+cluster://host1:6379,host2:6379")
```

Credentials in the URL userinfo apply to both the Sentinel connections
and the data connections. Use the factory methods when the Sentinel
password differs from the data password:

```python
RedisProvider.sentinel(
    sentinels=[("host1", 26379), ("host2", 26379)],
    service_name="mymaster",
    db=0,
    password="data-password",
    sentinel_kwargs={"password": "sentinel-password"},
)

RedisProvider.cluster(
    nodes=[("host1", 6379), ("host2", 6379)],
    password="cluster-password",
)
```

`safe_url` and `repr()` redact the password for every scheme, including
the multi-host forms.

### Failover on Sentinel

The Sentinel client re-resolves the master when it changes. During that
brief window an in-flight command can error. Wrap the call in the
[resilience](resilience/index.md) patterns (retry and circuit breaker)
to ride through the failover.

### The hash-tag rule on Cluster

A Redis Cluster shards keys across slots by a hash of the key. A command
or script that touches several keys must keep them in one slot, or the
cluster rejects it as a cross-slot error.

The cache adapter and the lock adapter both run multi-key operations.
On Cluster, give their `prefix` a hash tag so every key they touch lands
in one slot. A hash tag is any substring in braces: the cluster hashes
only what is inside the first `{...}`.

```python
provider = RedisProvider("redis+cluster://host1:6379,host2:6379")
cache = provider.cache(prefix="{myapp}cache")
lock = provider.lock(prefix="{myapp}")
```

Without a hash tag, the adapter raises a `ValueError` at construction
that names the fix. Standalone and Sentinel need no hash tag, since every
key lives on one server. The rate limiter, circuit breaker, schedule, and
leader-election adapters touch one key per call and work on Cluster as is.

## Factory methods

Each Provider exposes factory methods that return its matching adapter:

| Method                      | Returns                       | RedisProvider | ValkeyProvider | PostgresProvider | SQLiteProvider | MemoryProvider |
|----------------------------|-------------------------------|:-------------:|:--------------:|:----------------:|:--------------:|:--------------:|
| `.lock(**kwargs)`           | `LockBackend` implementation  |       ✓        |       ✓        |        ✓         |       ✓        |       ✓        |
| `.schedule(**kwargs)`       | `ScheduleBackend` impl        |       ✓        |       ✓        |        ✓         |       ✓        |       ✓        |
| `.leaderelection(**kwargs)` | `LeaderElectionBackend` impl  |       ✓        |       ✓        |        ✓         |      N/A       |       ✓        |
| `.cache(**kwargs)`          | `CacheBackend` implementation |       ✓        |       ✓        |        ✓         |       ✓        |       ✓        |
| `.ratelimiter(**kwargs)`    | `RateLimiterBackend` impl     |       ✓        |       ✓        |        ✓         |       ✓        |       ✓        |
| `.circuitbreaker(**kwargs)` | `CircuitBreakerBackend` impl  |       ✓        |       ✓        |        ✓         |       ✓        |       ✓        |

Factories that do not apply raise `NotImplementedError` with a message
pointing to the right alternative. `Coordination(provider)`, `Cache(provider)`,
`RateLimiterRegistry(provider)`, and `CircuitBreakerRegistry(provider)` call these factories.

## Readiness check

Every connection provider ships a built-in `check()` readiness probe: Redis and
Valkey run `PING`, Postgres and SQLite run `SELECT 1`, and Memory returns
ready right away. A `HealthChecks` registers it as a
`provider:{short_name}` check, one provider at a time with
`health.add_provider(provider)` or for the whole app with
`HealthChecks(auto_health=True)`. See [Health Checks](health.md#provider-readiness-checks).

## Valkey

`ValkeyProvider` is a subclass of `RedisProvider`. It connects to a
[Valkey](https://valkey.io) server using the `valkey-py` client
(`valkey.asyncio`) and serves the same adapter set as `RedisProvider`:
Lock, LeaderElection, Schedule, TTLCache, RateLimiter, and CircuitBreaker.

Install the `valkey` extra before using it:

```bash
pip install "grelmicro[valkey]"
```

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.coordination import Coordination
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.resilience import RateLimiterRegistry

valkey = ValkeyProvider("redis://localhost:6379/0")

micro = Grelmicro(uses=[
    Coordination(valkey),
    Cache(valkey),
    RateLimiterRegistry(valkey),
])
```

Set `VALKEY_URL` (or `VALKEY_HOST` + `VALKEY_PORT` + `VALKEY_DB` +
`VALKEY_PASSWORD`) for env-driven construction.

Construction forms:

```python
ValkeyProvider("redis://localhost:6379")     # positional URL
ValkeyProvider(url="redis://...")            # keyword URL
ValkeyProvider(host="x", port=6379, db=0)   # decomposed kwargs
ValkeyProvider()                             # env-driven (VALKEY_*)
ValkeyProvider(env_prefix="CACHE_VALKEY_")  # custom env prefix
ValkeyProvider(env_load=False)              # kwargs only, no env
ValkeyProvider.from_config(RedisConfig(...))  # from a config object
ValkeyProvider.from_client(client)           # bring-your-own client
```

`ValkeyProvider` reads the same `redis+sentinel://` and `redis+cluster://`
schemes as `RedisProvider` and builds the Valkey Sentinel and Cluster
clients. The factory methods `ValkeyProvider.sentinel(...)` and
`ValkeyProvider.cluster(...)` and the Cluster hash-tag rule apply the
same way.

## Postgres

`PostgresProvider` ships all factory methods: `.lock()`, `.leaderelection()`, `.cache()`, `.ratelimiter()`, `.circuitbreaker()`, and `.schedule()`. The
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

Set `POSTGRES_URL` (or `POSTGRES_HOST` + `POSTGRES_PORT` + `POSTGRES_DB`
+ `POSTGRES_USER` + `POSTGRES_PASSWORD`) for env-driven construction.

For two pools (writer + reader), split by env prefix:

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

## SQLite

`SQLiteProvider` ships the `.lock()`, `.ratelimiter()`, `.cache()`, `.circuitbreaker()`, and `.schedule()` factories. The
provider owns one `aiosqlite` connection (autocommit, WAL) and a shared
lock that adapters borrow.

```python
from grelmicro import Grelmicro
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import RateLimiterRegistry

sqlite = SQLiteProvider("app.db")

micro = Grelmicro(uses=[
    RateLimiterRegistry(sqlite),
])
```

Set `SQLITE_PATH` for env-driven construction. Construction forms:

```python
SQLiteProvider("app.db")                  # positional path
SQLiteProvider(path="app.db")             # keyword path
SQLiteProvider()                          # env-driven (SQLITE_PATH)
SQLiteProvider(env_prefix="CACHE_SQLITE_")  # custom env prefix
SQLiteProvider(env_load=False)            # kwargs only, no env
SQLiteProvider.from_config(SQLiteConfig(...))
SQLiteProvider.from_client(connection)    # bring-your-own connection
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

## Memory

`MemoryProvider` ships every factory: `.lock()`, `.leaderelection()`,
`.schedule()`, `.cache()`, `.ratelimiter()`, and `.circuitbreaker()`. It owns no
connection. State lives in process and disappears on restart, so it is for
tests and single-process apps. Reach for Redis, Postgres, or SQLite for
durable, distributed coordination.

```python
from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.coordination import Coordination
from grelmicro.providers.memory import MemoryProvider
from grelmicro.resilience import CircuitBreakerRegistry, RateLimiterRegistry

memory = MemoryProvider()

micro = Grelmicro(uses=[
    memory,
    Coordination(lock=memory.lock(), election=memory.leaderelection()),
    Cache(memory.cache()),
    RateLimiterRegistry(memory.ratelimiter()),
    CircuitBreakerRegistry(memory.circuitbreaker()),
])
```

Each factory hands back one cached adapter per kind, so the provider owns a
single in-process store per kind. `memory.lock()` called twice returns the same
backend, so a later call re-fetches the live store for a test or an
introspection. Wire each kind into one component, the same way you would a Redis
adapter. A lone `MemoryProvider` resolves every kind, so
`uses=[memory, Coordination(memory)]` wires the lock, election, and schedule
backends from it.

To wire a single component, pass the provider straight in:

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.providers.memory import MemoryProvider

memory = MemoryProvider()

micro = Grelmicro(uses=[
    Coordination(memory),
])
```

You can still pass a raw adapter (`MemoryLockAdapter`, `MemoryCacheAdapter`, ...)
to its Component when you do not want a provider.
