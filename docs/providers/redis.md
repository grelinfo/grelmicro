# Redis and Valkey

`RedisProvider` serves every kind: Lock, LeaderElection, Schedule, TTLCache,
RateLimiter, and CircuitBreaker. It wraps a `redis.asyncio` client and opens the
pool on `__aenter__`.

```python
from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(uses=[redis])
```

Install the `redis` extra first: `pip install "grelmicro[redis]"`.

## Environment variables

Construct the provider with no arguments and it reads its connection from the
environment. `GREL_ENV_LOAD=0` leaves these reads on, because they are the
provider's own connection variables:

| Environment variable | Description | Default |
|---|---|---|
| `REDIS_URL` | Full Redis URL (e.g. `redis://localhost:6379/0`) | |
| `REDIS_HOST` | Redis hostname | |
| `REDIS_PORT` | Redis port | `6379` |
| `REDIS_DB` | Redis database number | `0` |
| `REDIS_PASSWORD` | Redis password | |
| `REDIS_SENTINEL_PASSWORD` | Sentinel `requirepass`, for `redis+sentinel://` only | |

Set either `REDIS_URL` or `REDIS_HOST`, not both. Pass `env_prefix=` for a
different prefix, see [Recipe 2](index.md#recipe-2-split-pools-by-env-prefix).

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

`from_client` takes any `redis.asyncio.Redis`, including the subclass an
ORM or ODM layer hands back. The provider uses the client as it is, so it
shares the connection pool that layer already opened.

## Key prefix

Pass `prefix=` to keep every key of your service under one namespace. Every
store built on the provider puts it before its own `prefix`: the lock,
read-write lock, leader election, schedule, cache, rate limiter, circuit
breaker and job stores.

```python
redis = RedisProvider("redis://localhost:6379/0", prefix="myapp:")

redis.schedule_backend()             # keys under "myapp:"
redis.lock_backend()                 # keys under "myapp:lock:"
redis.cache_backend(prefix="users:") # keys under "myapp:users:cache:"
```

The lock, read-write lock, cache and job stores add their kind after the
prefixes, so no store reaches into the keys of another. A cache `clear()`
deletes only the cache keys, never a lock beside them. The other stores
already mark their keys, such as `myapp:cb:` for the circuit breaker.

| Store | No provider prefix | `prefix="myapp:"` |
|---|---|---|
| Lock | `cart`, `fence:cart` | `myapp:lock:cart`, `myapp:lock:fence:cart` |
| Read-write lock | `cart`, `cart:r`, `cart:i` | `myapp:rwlock:cart`, ... |
| Cache | `user:1`, `cache:tag:users` | `myapp:cache:user:1`, ... |
| Job | `job:deadlines`, `job:id:...` | `myapp:job:deadlines`, `myapp:job:id:...` |
| Leader election | `le_us:primary` | `myapp:le_us:primary` |
| Schedule | `nightly` | `myapp:nightly` |
| Rate limiter | `tb:...`, `gcra_us:...` | `myapp:tb:...`, `myapp:gcra_us:...` |
| Circuit breaker | `cb:payments` | `myapp:cb:payments` |

The default is empty, so no key changes. `RedisConfig` and every
construction form take the same `prefix`. It is never read from the
environment, so setting `REDIS_PREFIX` moves no key. `micro.describe()` shows
it under each provider. A `{` that opens no
[hash tag](#the-hash-tag-rule-on-cluster) is refused with
`SettingsValidationError` on every topology, and so is a `}` that closes
none.

## Sentinel and Cluster

`RedisProvider` switches topology from the URL scheme. The scheme rides
the same `url` field, so `REDIS_URL` alone selects standalone, Sentinel,
or Cluster with no other code change.

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

Every store keeps working on Cluster as it is, with no prefix to set. See
[the hash-tag rule](#the-hash-tag-rule-on-cluster) for where its keys go.

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
[resilience](../resilience/index.md) patterns (retry and circuit breaker)
to ride through the failover.

### The hash-tag rule on Cluster

A Redis Cluster shards keys across slots by a hash of the key. A command
or script that touches several keys must keep them in one slot, or the
cluster rejects it as a cross-slot error. A hash tag is a name in braces:
the cluster hashes only what is inside the first `{...}`.

The cache, lock, read-write lock and job stores touch several keys at once.
On Cluster, each one puts its keys under a hash tag of its own when no
prefix carries one. The tag is the provider prefix, then the store prefix,
then the store kind:

| Store | No provider prefix | `prefix="myapp:"` | `prefix="{myapp}:"` |
|---|---|---|---|
| Lock | `{lock}:cart` | `{myapp:lock}:cart` | `{myapp}:lock:cart` |
| Read-write lock | `{rwlock}:cart` | `{myapp:rwlock}:cart` | `{myapp}:rwlock:cart` |
| Cache | `{cache}:user:1` | `{myapp:cache}:user:1` | `{myapp}:cache:user:1` |
| Job | `{job}:id:...` | `{myapp:job}:id:...` | `{myapp}:job:id:...` |

Each store keeps its keys in one slot, and the stores spread across
shards. A store prefix joins the tag too: `cache_backend(prefix="users:")`
with `prefix="myapp:"` writes under `{myapp:users:cache}:`.

To place a store yourself, put a tag in its prefix. With no provider
prefix, a store prefix that carries a tag is used as it is, so these keys
stay byte for byte:

```python
provider = RedisProvider("redis+cluster://host1:6379,host2:6379")
cache = provider.cache_backend(prefix="{myapp}cache")
lock = provider.lock_backend(prefix="{myapp}")
jobs = provider.job_backend(prefix="{myapp}jobs:")
```

A tag in the provider prefix, such as `prefix="{myapp}:"`, puts every
store of the provider in that one slot. Each multi-key store still adds its
kind after it.

A brace that opens or closes no tag, such as `"{myapp"` or `"a}b"`, is
refused in the provider prefix with `SettingsValidationError`, on every
topology. A store prefix is refused only on Cluster, for the four stores
above, when it has braces but no `{name}` anywhere. Keep the first `{` of a
store prefix around a name: in `"{}{app}"` the cluster reads no tag, and
the store fails with a cross-slot error.

The rate limiter, circuit breaker, schedule and leader-election stores
touch one key per call. They never take a tag, so their keys spread across
shards. Standalone and Sentinel keys never take a tag, since every key
lives on one server.

CI runs these stores on a real Redis Cluster and a real Valkey Cluster.
The [capability matrix](../capabilities.md#patterns-on-a-backend) gives
the level of each one.

## Valkey

`ValkeyProvider` is a subclass of `RedisProvider`. It connects to a
[Valkey](https://valkey.io) server using the `valkey-py` client
(`valkey.asyncio`) and serves the same adapter set.

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
