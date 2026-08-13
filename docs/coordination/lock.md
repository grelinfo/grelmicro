# Lock

The lock is a distributed lock that synchronizes access to a shared resource.

The lock supports the following features:

- **Async**: the lock is acquired and released asynchronously.
- **Distributed**: the lock is shared across multiple workers.
- **Non-reentrant**: a nested acquire from the same task or thread raises
  `LockReentrantError`. Use separate instances if you need independent locks.
- **Idempotent backend**: the backend lets the same token re-acquire the lock,
  which extends the lease. Call `extend()` if you need to extend the
  lease explicitly.
- **Expiring**: the lock has a timeout that auto-releases the lock to prevent
  deadlocks.
- **Non-blocking**: lock operations do not block the async event loop.
- **Backend-agnostic**: several backends are supported, including Redis,
  PostgreSQL, and Kubernetes ConfigMap.

```python
--8<-- "coordination/lock.py"
```

Load a backend first, see [Backends](index.md#backends).

!!! warning
    The lock is built for one async event loop and is not thread-safe or
    process-safe.

## Configuration

Build the lock with keyword arguments. The positional `name` is always required
and acts as the instance identity.

```python
--8<-- "coordination/lock_programmatic.py"
```

### Environment variables

Tune any field in deployment without code changes.

Prefix: `GREL_LOCK_{NAME_UPPER}_`. The default instance drops the name segment and reads `GREL_LOCK_*`.

--8<-- "env_gate.md"

| Env var                                      | Config field     | Type            | Default          |
|----------------------------------------------|------------------|-----------------|------------------|
| `GREL_LOCK_{NAME_UPPER}_WORKER`              | `worker`         | `str \| UUID`   | generated UUID   |
| `GREL_LOCK_{NAME_UPPER}_LEASE_DURATION`      | `lease_duration` | `float` (> 0)   | `60`             |
| `GREL_LOCK_{NAME_UPPER}_RETRY_INTERVAL`      | `retry_interval` | `float` (>= 0.001) | `0.1`         |
| `GREL_LOCK_{NAME_UPPER}_RETRY_JITTER`        | `retry_jitter`   | `float` [0, 1)     | `0.1`         |

Concrete example for `Lock("cart")`:

```bash
GREL_LOCK_CART_WORKER=web-1
GREL_LOCK_CART_LEASE_DURATION=120
GREL_LOCK_CART_RETRY_INTERVAL=0.2
GREL_LOCK_CART_RETRY_JITTER=0.2
```

The code stays the same, the environment fills the fields in:

```python
--8<-- "coordination/lock_environmental.py"
```

!!! tip "Advanced"
    For custom env prefixes with `env_prefix=`, the `from_config` declarative
    path, and `pydantic-settings` composition, see
    [Declarative configuration](../advanced/config.md).

## Dynamic-key Locks

Most Locks are declared once at module load (`lock = Lock("cart")`) and reused
across requests. When the lock key is computed per request, build a fresh `Lock`
each time:

```python
lock = Lock(f"order:{order_id}")
async with lock:
    ...
```

This is the right pattern when locking by business identity (`order_id`,
`user_id`, `tenant_id`).

!!! tip "Advanced"
    On a measured hot loop that builds many Locks per request, pre-build a single
    `LockConfig` and call `Lock.from_config(name, cfg)` to skip per-call
    validation and the env read. See
    [Declarative configuration](../advanced/config.md).

## Bounded acquire

Pass `timeout=` to `acquire()` to limit how long the call waits. When the
deadline passes without winning the lock, `TimeoutError` is raised:

```python
# Wait up to 5 seconds, then raise TimeoutError.
held = await lock.acquire(timeout=5.0)
```

The context manager (`async with lock`) calls `acquire()` with no timeout and
waits indefinitely. Use `acquire(timeout=...)` directly when you need a
bounded wait and want to handle the failure yourself.

## Extending the lease

Call `extend()` on a `Lock` to renew the TTL without releasing the lock. The
fencing token stays the same, only the expiry time advances:

```python
lock = Lock("cart")
async with lock as held:
    token_before = held.fencing_token
    extended = await lock.extend()
    assert extended.fencing_token == token_before  # same token, new TTL
```

`extend()` raises `LockNotOwnedError` when the lease was lost on the backend
(expired or taken over by another holder).

## Fencing tokens

A fencing token is a strictly increasing integer the backend mints for a lock
name. Each acquisition returns a `LockHandle` that carries it. Read it from the
value the context manager binds:

```python
async with Lock("cart") as held:
    print(held.fencing_token)
```

The token grows by one on every free-to-held transition: a new holder, or a
takeover after the previous lease expired. It keeps climbing across release and
re-acquire cycles, so a token is never reused for a name. The same holder
renewing or extending its lease keeps the same token.

`acquire()` and `acquire_nowait()` also return the `LockHandle`. The handle is
per-acquisition, so a `Lock` shared by several tasks gives each holder its own
handle with its own token.

Every backend mints tokens that are strictly monotonic per name. Redis is
strictly monotonic against its master.

!!! warning "The resource enforces, grelmicro mints"
    A fencing token only protects a resource that checks it. grelmicro hands you
    the token. The resource you write to must record the highest token it has
    accepted and reject any write that arrives with a lower or equal token.
    Without that check on the resource, a paused or partitioned old holder can
    still write after a new holder took over.

    The pattern: read `held.fencing_token`, pass it to the resource on every
    write, and have the resource compare it against its stored high-water mark.

```python
--8<-- "coordination/fencing.py"
```

!!! tip "Want to understand how worker identity and lock tokens work internally?"
    See [Coordination Internals](../architecture/coordination.md) for details on
    UUID generation, token scoping, and design guarantees.
