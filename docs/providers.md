# Providers

A **Provider** is a first-class connection object. It owns the vendor URL,
the native client (a Redis pool, an asyncpg pool, ...), and the lifecycle
of both. Components like `Coordination`, `Cache`, and `RateLimiterComponent` accept a
Provider directly and use its matching adapter under the hood, and a Provider
listed on its own registers one of each for you.

Five providers ship today: `RedisProvider`, `ValkeyProvider`, `PostgresProvider`,
`SQLiteProvider`, and `MemoryProvider`. More will follow.

## Recommended shape

List the Provider and nothing else:

```python
from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(uses=[redis])

async with micro:
    ...
```

That registers `Coordination`, `Cache`, `RateLimiterComponent`, and
`CircuitBreakerComponent`, all sharing the one pool. Each Component dispatches
to the Provider's factory methods (`provider.lock()`, `provider.cache()`,
`provider.ratelimiter()`). The Adapter classes (`RedisLockAdapter`,
`RedisCacheAdapter`, `RedisRateLimiterAdapter`) stay public as escape hatches
but rarely appear in user code.

!!! note "Import policy: prefer Providers over concrete adapters"
    App code should import a Provider and pass it to Components, not import
    concrete adapter classes. The Provider owns the connection and
    hands each Component the right adapter, so one URL change swaps every
    backend at once. Import an adapter directly only for an escape hatch: a
    bespoke client the factory does not build, or a per-process Memory backend
    in a test. Adapters live in their backend submodule
    (`grelmicro.resilience.circuitbreaker.sqlite`) and the top-level package
    re-exports them (`from grelmicro.resilience import SQLiteCircuitBreakerAdapter`).

!!! tip "Name a Component to override one kind"
    A Component claims its own kind and the Provider fills the rest, so one
    entry moves one capability:

    ```python
    micro = Grelmicro(uses=[redis, Cache(postgres)])
    ```

    Everything stays on Redis except the cache. A bare backend works the same
    way and is wrapped in its Component for you:

    ```python
    micro = Grelmicro(uses=[redis, MemoryCircuitBreakerAdapter()])
    ```

    A Provider held by a Component is discovered and lifecycled for you, so
    you can drop the top-level entry once every kind is spelled out. The
    shared `redis` opens once, before the Components that hold it. Listing it
    explicitly lets you control where it sits in the lifecycle order.

## Recipe 1: env-driven

Construct the Provider without arguments and let it read `REDIS_*` from
the environment:

```python
from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider()  # reads REDIS_URL or REDIS_HOST + REDIS_PORT + ...

micro = Grelmicro(uses=[redis])
```

Set `REDIS_URL` (or `REDIS_HOST` + `REDIS_PORT` + `REDIS_DB` +
`REDIS_PASSWORD`) in the environment.

These reads need no flag. `GREL_ENV_LOAD` gates the `GREL_*` variables that
tune components, not a Provider's own connection variables. Pass
`env_load=False` to build a Provider from keyword arguments alone.

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

## Recipe 4: the managed connection, nothing else

Not all of your data is a cache, a lock, or a rate limiter. Plain application
state in a Redis hash is still yours to read and write, and a Provider gives you
that connection with the lifecycle already handled. Reach for `provider.client`:

```python
--8<-- "wiring/provider_client.py"
```

The client is the native one (`redis.asyncio.Redis`, `asyncpg.Pool`,
`aiosqlite.Connection`), so every command that library offers is available. The
app opens it on startup and closes it on shutdown, and a `HealthChecks` can probe
it with `add_provider(redis)`.

Two things to know. The client exists only inside the app scope, so a call made
before startup raises `OutOfContextError`. And a lone Provider listed with no
components also registers a default component per kind it serves, which costs
nothing if you never use them. List the components you do want to be explicit
about the wiring.

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

## URL validation

A URL reaches a provider from three places: the constructor, the
environment, and a config object. All three check it against the same
type, so a URL accepted in one is accepted in the others, and a URL
refused in one is refused in all of them.

A URL the provider cannot use is refused before any client is built:

```python
RedisProvider("anything://localhost:6379")
# RedisProviderConfigError: Could not validate settings:
# - url: URL scheme should be 'redis', 'rediss', 'unix', 'redis+sentinel' or 'redis+cluster'
```

`RedisProviderConfigError` and `PostgresProviderConfigError` are both
`GrelmicroError`, so one `except` covers every way the URL arrives.

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

### Authenticated Sentinels

Sentinel servers often run with their own `requirepass`, and the Bitnami
Redis chart turns that on by default. That password is separate from the
data password, and both fit in the environment:

```bash
REDIS_URL=redis+sentinel://sentinel-0:26379,sentinel-1:26379/mymaster/0
REDIS_PASSWORD=...
REDIS_SENTINEL_PASSWORD=...
```

```python
provider = RedisProvider()
```

It applies only when set. The data password is never reused for the
Sentinel connections, because `AUTH` against a server without
`requirepass` fails, which would break every deployment whose Sentinels
are unauthenticated.

It also applies only to a `redis+sentinel://` URL. Setting it alongside
any other scheme warns rather than passing silently, since one
environment often serves several services and only some of them talk to
Sentinel.

The same value is available as `sentinel_password=` on the constructor
and on `RedisConfig`.

Credentials in the URL userinfo apply to both the Sentinel connections
and the data connections. Use the factory methods when you need to pass
other Sentinel connection settings:

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

## Credentials in a config object

A connection URL carries its password inside itself, so a config object
holding one would print it. The `url` field on `PostgresConfig` and
`RedisConfig` is a
[`SecretUrl`][grelmicro.types.SecretUrl]: it displays the URL with every
credential replaced by `***`, and hands back the real value only through
`get_secret_value()`.

```python
from grelmicro.providers.redis import RedisConfig

config = RedisConfig(url="redis://app:hunter2@cache:6379/0")

print(repr(config))
# RedisConfig(url=SecretUrl('redis://app:***@cache:6379/0'), host=None, ...)

print(config.url.get_secret_value())
# redis://app:hunter2@cache:6379/0
```

Building the config is unchanged: pass a plain string and grelmicro
wraps it. The scheme, host, port, and database stay readable, so a log
line still tells an operator which server the app is talking to.

The same masking covers `repr()`, `model_dump()`, and
`model_dump_json()`. Passing the config to `from_config()` uses the real
URL, and so does every connection grelmicro opens.

A rejected value is never quoted back either. A mistyped URL would carry
its password into the `ValidationError` text, so these configs report the
failing field without the input.

!!! warning "Do not persist a config through JSON"
    `model_dump_json()` writes the masked form. Reloading that output
    gives you a config whose password is the literal `***`, and the
    connection then fails to authenticate. Persist the value from
    `get_secret_value()` instead, or keep the credential in the
    environment and let the provider read it. This matches how `SecretStr`
    already behaves for the `password` field.

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
`RateLimiterComponent(provider)`, and `CircuitBreakerComponent(provider)` call these factories.

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
from grelmicro.providers.valkey import ValkeyProvider

valkey = ValkeyProvider("valkey://localhost:6379/0")

micro = Grelmicro(uses=[valkey])
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
ValkeyProvider.from_config(ValkeyConfig(...))  # from a config object
ValkeyProvider.from_client(client)           # bring-your-own client
```

`ValkeyProvider` also reads Valkey's own schemes, so a deployment can name
the server it runs: `valkey://`, `valkeys://`, `valkey+sentinel://`, and
`valkey+cluster://` work wherever the `redis` spellings do, in the
constructor, in `VALKEY_URL`, and in `ValkeyConfig`. `RedisProvider` and
`RedisConfig` keep the `redis` schemes only.

`ValkeyConfig` carries the same fields as `RedisConfig`, so
`ValkeyProvider.from_config()` takes either one.

`ValkeyProvider` reads the same `redis+sentinel://` and `redis+cluster://`
schemes as `RedisProvider` and builds the Valkey Sentinel and Cluster
clients. The factory methods `ValkeyProvider.sentinel(...)` and
`ValkeyProvider.cluster(...)` and the Cluster hash-tag rule apply the
same way.

## Postgres

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

A SQLAlchemy-style URL works as it is. The provider drops the driver
suffix, so an app already holding `postgresql+asyncpg://localhost/app`
passes it straight through with no string surgery:

```python
postgres = PostgresProvider("postgresql+asyncpg://localhost/app")
```

The suffix names the client library that app uses, not the wire
protocol, and this provider always connects with asyncpg.

Set `POSTGRES_URL` (or `POSTGRES_HOST` + `POSTGRES_PORT` + `POSTGRES_DB`
+ `POSTGRES_USER` + `POSTGRES_PASSWORD`) for env-driven construction. The
database name also reads from `POSTGRES_DATABASE` when `POSTGRES_DB` is
unset, so both the `postgres` Docker image convention and the longer
spelling work.

Pass `command_timeout` (or set `POSTGRES_COMMAND_TIMEOUT`) to bound every operation. A query that hangs on a frozen or unreachable server then raises `TimeoutError` after that many seconds, instead of blocking until the OS TCP timeout. It defaults to `None` (no timeout).

```python
postgres = PostgresProvider("postgresql://localhost/app", command_timeout=5)
```

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

sqlite = SQLiteProvider("app.db")

micro = Grelmicro(uses=[sqlite])
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

**Order does not matter.** A Provider opens before the Components that
borrow it, wherever you list it, and a Provider you leave out entirely is
discovered and opened for you. `uses=` says what the app is made of, and
grelmicro opens it in dependency order.

This matters because the resource is often lazy: `PostgresProvider` builds
its `asyncpg.Pool` on `__aenter__`, so a Component that opened first would
reach for `provider.client` before the pool exists.

Listing the Provider first still reads well and is worth doing for a human
reader. If you want the list you wrote to be exactly the list that runs,
`Grelmicro(strict=True)` raises `LifecycleOrderError` instead of reordering.

## Memory

`MemoryProvider` ships every factory: `.lock()`, `.leaderelection()`,
`.schedule()`, `.cache()`, `.ratelimiter()`, and `.circuitbreaker()`. It owns no
connection. State lives in process and disappears on restart, so it is for
tests and single-process apps. Reach for Redis, Postgres, or SQLite for
durable, distributed coordination.

```python
from grelmicro import Grelmicro
from grelmicro.providers.memory import MemoryProvider

memory = MemoryProvider()

micro = Grelmicro(uses=[memory])
```

Each factory hands back one cached adapter per kind, so the provider owns a
single in-process store per kind. `memory.lock()` called twice returns the same
backend, so a later call re-fetches the live store for a test or an
introspection. Reach for a factory when a test needs the live store, or when
one kind should run on a different backend than the rest.

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
