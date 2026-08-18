# Providers

A **Provider** is a first-class connection object. It owns the vendor URL,
the native client (a Redis pool, an asyncpg pool, ...), and the lifecycle
of both. Components like `Coordination`, `Cache`, and `RateLimiterComponent` accept a
Provider directly and use its matching adapter under the hood, and a Provider
listed on its own registers one of each for you.

Five providers ship today: [`RedisProvider`](redis.md), [`ValkeyProvider`](redis.md#valkey),
[`PostgresProvider`](postgres.md), [`SQLiteProvider`](#sqlite), and
[`MemoryProvider`](#memory). More will follow.

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

Every Provider takes the same shapes. Redis as the example:

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
# SettingsValidationError: Could not validate settings:
# - url: URL scheme should be 'redis', 'rediss', 'unix', 'redis+sentinel' or 'redis+cluster'
```

Every provider raises `SettingsValidationError`, the same error every other
class raises for a bad value, so one `except` covers every way the URL
arrives.

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
`HealthChecks(auto_health=True)`. See [Health Checks](../health.md#provider-readiness-checks).

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
