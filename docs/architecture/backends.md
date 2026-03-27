# Backend Registry

Grelmicro uses a shared backend registry pattern to make infrastructure backends (Redis, PostgreSQL, SQLite, etc.) swappable without changing application code.

## Why Async

All backends use **async** methods because they perform network or disk I/O (Redis, PostgreSQL, SQLite, Kubernetes API). Async avoids blocking the event loop, which is critical in microservice applications handling many concurrent requests. Even backends with low-latency I/O (like SQLite) use async to maintain a consistent interface and allow the event loop to schedule other work during I/O waits.

## Design

The `BackendRegistry[T]` is a generic, typed container that holds a single default backend instance. Each module maintains its own registry:

| Module | Registry | Protocol | Backends |
|---|---|---|---|
| `sync` | `sync_backend_registry` | `SyncBackend` | Redis, PostgreSQL, SQLite, Kubernetes, Memory |
| `cache` | `cache_backend_registry` | `CacheBackend` | Redis |

## Registration

Backends register themselves on initialization via `auto_register=True` (the default):

```python
async with RedisSyncBackend() as backend:
    # backend is now the default for Lock, TaskLock, LeaderElection
    ...
```

This calls `sync_backend_registry.set(self)` internally. Consumers that accept an optional `backend` parameter fall back to the registry when none is provided:

```python
# Explicit backend
lock = Lock(name="my-lock", backend=my_backend)

# Uses the registered default
lock = Lock(name="my-lock")
```

## Protocol-Based Polymorphism

Backends are defined by protocols (structural typing), not base classes. Any object implementing the required methods works as a backend. This enables:

- Swapping backends without changing application code
- Writing test backends (e.g. `MemorySyncBackend`) with no external dependencies
- Adding new backends without modifying existing code

## Auto-Registration Control

Set `auto_register=False` to create a backend without registering it as the default. This is useful for:

- Tests that need isolated backend instances
- Applications using multiple backends for different purposes
- Manual wiring in dependency injection setups

```python
# Not registered as default
cache = RedisCache(url="redis://localhost/1", ttl=60, auto_register=False)
```

## Error Handling

Accessing a registry before any backend is registered raises `BackendNotLoadedError` with a descriptive message:

```
No lock backend loaded. Initialize a backend first
(e.g. with ``async with`` a backend instance).
```
