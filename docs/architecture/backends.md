# Backend Registry

grelmicro uses a shared backend registry pattern to make infrastructure backends (Redis, PostgreSQL, SQLite, etc.) swappable without changing application code.

## Why Async

All backends use **async** methods because they perform network or disk I/O (Redis, PostgreSQL, SQLite, Kubernetes API). Async avoids blocking the event loop, which is critical in microservice applications handling many concurrent requests. Even backends with low-latency I/O (like SQLite) use async to maintain a consistent interface and allow the event loop to schedule other work during I/O waits.

## Design

`BackendRegistry[T]` is a generic, typed, multi-name container with task-scoped overrides. Each module maintains its own registry. The registry key matches the module name and is the value to use in `lifespan(exclude={...})`:

| Module | Registry key | Protocol / Type | Backends |
|---|---|---|---|
| `grelmicro.sync` | `sync` | `SyncBackend` | Redis, PostgreSQL, SQLite, Kubernetes, Memory |
| `grelmicro.cache` | `cache` | `CacheBackend` | Redis, Memory |
| `grelmicro.resilience` | `resilience` | `RateLimiterBackend` | Redis, Memory |
| `grelmicro.health` | `health` | `HealthRegistry` | (any number of named registries) |

A registry holds zero or more named entries. Backends are looked up by name at call time, and each entry is independent. The `"default"` slot is the implicit name when no `backend=` is passed.

## Construction vs registration

Construction and registration are two distinct steps. `__init__` is pure: it validates configuration and binds locals. It performs no registry writes and no I/O. Registration is explicit and reversible.

There are three ways to wire a backend:

**1. Register and open via `grelmicro.lifespan()` (recommended for apps).** Register synchronously at startup, then open every registered backend with one call:

```python
import grelmicro
from grelmicro import sync, cache

sync.register(RedisSyncBackend())             # implicit "default"
cache.register(RedisCacheBackend())

async with grelmicro.lifespan():
    # every registered backend is open here
    ...
# every registered backend is closed on exit (LIFO)
```

`grelmicro.lifespan()` walks every grelmicro registry that has been imported in the current process, opens each entry via its async context manager, and closes them in reverse order on exit. Use `exclude={"<module>"}` to skip a whole module or `exclude={"<module>.<name>"}` to skip one entry.

**2. Module-level `use_backend` shorthand.** Equivalent to `register("default", backend)`:

```python
from grelmicro import sync

sync.use_backend(RedisSyncBackend())
```

Available as `grelmicro.sync.use_backend`, `grelmicro.cache.use_backend`, `grelmicro.resilience.use_backend`, and `grelmicro.health.use_registry`.

**3. Pure construction with explicit pass-through.** Skip the registry entirely:

```python
async with RedisSyncBackend() as backend:
    lock = Lock(name="my-lock", backend=backend)
    async with lock:
        ...
```

`async with` opens the connection only. The backend is not registered.

## Named backends and per-call selection

Register multiple backends under different names and pick one at the call site:

```python
sync.register(RedisSyncBackend())                                # → "default"
sync.register(PostgresSyncBackend("postgres://..."), "analytics")

Lock("cart")                         # → "default" (Redis)
Lock("audit", backend="analytics")   # → "analytics" (Postgres)
Lock("cart", backend=my_instance)    # → explicit instance, bypasses names
```

Resolution order, in priority:

1. Explicit instance (`backend=instance`).
2. Task-scoped override for the requested name (set via `<module>.use(...)`).
3. Registered entry under the requested name.
4. When the requested name is `"default"` and exactly one backend is registered: that sole entry.
5. Otherwise raise `BackendNotLoadedError`.

## Task-scoped overrides

`<module>.use(...)` installs a per-task override for the duration of a `with` block. Stacks LIFO via `contextvars`:

```python
from grelmicro import sync

with sync.use(MemorySyncBackend()):           # overrides "default" only
    Lock("cart")                              # → MemorySyncBackend

with sync.use(default=mem, analytics=fake):   # overrides multiple names
    ...

# Tests
async def test_checkout():
    with sync.use(MemorySyncBackend()):
        await checkout()
```

The override propagates downward through `await`, `asyncio.create_task`, and `asyncio.to_thread` (asyncio copies the calling context at every concurrency boundary). Set the override on the side that *calls into* the registry: an override set inside a worker thread is invisible to `from_thread.run` callbacks (which run on the loop's context).

## Lazy registration

Each `BackendRegistry` subscribes itself into a process-wide map *when its module is imported*. Modules you never import never create their registry, never appear in `grelmicro.lifespan()`, and never consume RAM. `import grelmicro` alone is ~6 ms. The per-component cost is paid only when the user imports that component.

## Identity-checked unregister

`registry.unregister(name, backend)` clears the entry only when the registered instance is identical to the one passed in. Calling on a non-current instance is a no-op. A stale backend's teardown cannot evict a newer backend that replaced it under the same name.

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
No sync backend loaded for name 'default'.
```

Or, when multiple backends are registered without a `"default"`:

```
No default sync backend: multiple are registered
(['analytics', 'primary']), none named 'default'.
```
