# Providers

A **Provider** is a first-class connection object. It owns the vendor URL,
the native client (a Redis pool, an asyncpg pool, ...), and the lifecycle
of both. Components like `Sync` and `Cache` borrow a client from a
provider instead of opening their own, so two components against the
same vendor share one connection.

The first provider is `RedisProvider`. More providers will follow.

## Recipe 1: env-driven, implicit sharing

The most common shape. Construct adapters without arguments and let them
build their own provider from `REDIS_*` environment variables.
`Grelmicro` dedupes implicit providers by `(provider_class, env_prefix)`,
so a single connection feeds both components:

```python
from grelmicro import Grelmicro
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.sync.redis import RedisSyncAdapter

micro = Grelmicro(uses=[
    RedisSyncAdapter(),
    RedisCacheAdapter(),  # shares the sync adapter's RedisProvider
])

async with micro:
    ...
```

Set `REDIS_URL` (or `REDIS_HOST` + `REDIS_PORT` + `REDIS_DB` +
`REDIS_PASSWORD`) in the environment.

## Recipe 2: explicit provider

Build the provider yourself when you want to read its `.url` or share it
beyond grelmicro:

```python
from grelmicro.providers.redis import RedisProvider

provider = RedisProvider("redis://localhost:6379")

micro = Grelmicro(uses=[
    RedisSyncAdapter(provider=provider),
    RedisCacheAdapter(provider=provider),
])
```

An explicit `provider=` is borrowed, not owned. The caller drives its
lifecycle.

## Recipe 3: split pools by env prefix

Two Redis instances (or two databases) live behind different prefixes.
Each prefix gets its own shared provider:

```python
cache_adapter = RedisCacheAdapter(env_prefix="CACHE_REDIS_")
session_adapter = RedisSyncAdapter(env_prefix="SESSION_REDIS_")

micro = Grelmicro(uses=[cache_adapter, session_adapter])
```

Set `CACHE_REDIS_URL` and `SESSION_REDIS_URL` (or the decomposed forms).
The two adapters now talk to two pools.

## Recipe 4: bring your own client

You already own a Redis client (custom retry, sentinel, auth, or a
testcontainers fixture). Wrap it with `from_client`:

```python
import redis.asyncio as redis
from grelmicro.providers.redis import RedisProvider

client = redis.Redis(host="prod.cache", socket_timeout=5)
provider = RedisProvider.from_client(client)  # caller owns the client

micro = Grelmicro(uses=[RedisCacheAdapter(provider=provider)])
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

The builder methods `provider.sync(...)` and `provider.cache(...)` are
pure sugar over `RedisSyncAdapter(provider=provider, ...)` and
`RedisCacheAdapter(provider=provider, ...)`.
