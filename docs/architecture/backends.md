# Backend Registry

grelmicro uses a shared backend registry pattern to make infrastructure backends (Redis, PostgreSQL, SQLite, etc.) swappable without changing application code.

## Why Async

All backends use **async** methods because they perform network or disk I/O (Redis, PostgreSQL, SQLite, Kubernetes API). Async avoids blocking the event loop, which is critical in microservice applications handling many concurrent requests. Even backends with low-latency I/O (like SQLite) use async to maintain a consistent interface and allow the event loop to schedule other work during I/O waits.

## Design

The `BackendRegistry[T]` is a generic, typed container that holds a single default backend instance. Each module maintains its own registry:

| Module | Registry | Protocol | Backends |
|---|---|---|---|
| `sync` | `sync_backend_registry` | `SyncBackend` | Redis, PostgreSQL, SQLite, Kubernetes, Memory |
| `cache` | `cache_backend_registry` | `CacheBackend` | Redis, Memory |

## Construction vs registration

Construction and registration are two distinct steps. `__init__` is pure: it validates configuration and binds locals, but never touches the global registry. Registration is explicit and reversible.

There are three ways to register a backend:

**1. Scoped registration via `async with` (recommended).** `__aenter__` registers the backend, `__aexit__` unregisters it. The registry is empty after the context exits.

```python
async with RedisSyncBackend() as backend:
    # backend is the default for Lock, TaskLock, LeaderElection here
    ...
# unregistered on exit
```

**2. Process-lifetime registration via `use_backend`.** Each module exposes a small helper for `main()` or lifespan wiring:

```python
from grelmicro import sync

backend = RedisSyncBackend()
sync.use_backend(backend)  # idempotent on the same instance
```

The helpers are: `grelmicro.sync.use_backend`, `grelmicro.cache.use_backend`, `grelmicro.resilience.use_backend`, `grelmicro.health.use_registry`.

**3. Pure construction without registration.** Pass the backend explicitly to consumers and skip the registry entirely:

```python
backend = MemorySyncBackend()
lock = Lock(name="my-lock", backend=backend)
```

Consumers that accept an optional `backend` parameter fall back to the registry when none is provided.

### Identity-checked unregister

`registry.unregister(backend)` only clears the slot when the registered instance is identical to the one passed in. Calling it on a non-current instance is a no-op. This means a stale backend's `__aexit__` cannot evict a newer backend that replaced it.

## Protocol-Based Polymorphism

Backends are defined by protocols (structural typing), not base classes. Any object implementing the required methods works as a backend. This enables:

- Swapping backends without changing application code
- Writing test backends (e.g. `MemorySyncBackend`) with no external dependencies
- Adding new backends without modifying existing code

## Connection Pool Isolation

Each backend instance creates its own Redis client and connection pool, even when multiple backends point to the same Redis server. This is an intentional design choice:

- **Failure isolation**: a slow lock Lua script cannot starve cache reads (and vice-versa)
- **Independent lifecycle**: each backend opens and closes on its own schedule via `async with`
- **Independent tuning**: pool settings (`max_connections`, timeouts) can be configured per domain
- **No hidden coupling**: closing the cache backend does not affect sync locks

The overhead is negligible (a few extra TCP connections, created lazily) and this approach follows the Python ecosystem standard (Django, SQLAlchemy, and Celery all use separate connection pools per concern).

Shared Redis configuration (URL resolution from environment variables) is deduplicated in `grelmicro/_redis.py`, but each backend receives its own client instance.

## Error Handling

Accessing a registry before any backend is registered raises `BackendNotLoadedError` with a descriptive message:

```
No lock backend loaded. Initialize a backend first
(e.g. with ``async with`` a backend instance).
```
