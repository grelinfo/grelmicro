# Sync from thread

grelmicro is async-first. Every primitive exposes an async API. When a synchronous handler in the host framework needs to call a primitive, grelmicro provides a sync entry point: `lock.from_thread`, `task_lock.from_thread`, and `cb.from_thread` for the locks and circuit breaker, and the `@cached(...)` decorator on a `def` function for `TTLCache`. Each one signals the intent explicitly so async code is never accidentally promoted to sync.

## How it works

The synchronous handler runs in a worker thread, with no event loop in scope. The sync adapter cannot `await`, so it schedules the coroutine on the parent loop with `asyncio.run_coroutine_threadsafe(coro, loop).result()` and blocks until the result is ready.

The loop reference is captured on the **backend** when the backend is opened during lifespan startup:

```python
async def __aenter__(self) -> Self:
    self._loop = asyncio.get_running_loop()
    return self
```

Each primitive reads the loop through its bound backend (`self.backend._loop`) when its sync adapter is invoked. No globals, zero hot-path overhead on the async API.

## Usage

```python
from contextlib import asynccontextmanager

from grelmicro import Grelmicro
from grelmicro.coordination import Lock
from grelmicro.coordination.memory import MemoryLockAdapter

micro = Grelmicro(uses=[MemoryLockAdapter()])
lock = Lock("cart")


@asynccontextmanager
async def app_lifespan(app):
    async with micro:            # opens every registered adapter
        yield


@app.get("/async-route")
async def async_route():
    async with lock:
        ...


@app.get("/sync-route")
def sync_route():                # runs in a worker thread
    with lock.from_thread:
        ...
```

## Why every primitive has a backend (including CircuitBreaker)

`CircuitBreaker` performs no I/O today. It still has a backend so:

1. **Lifespan ownership.** The in-memory backend resets every breaker bound to it on close, so process-level state is freed deterministically.
2. **Loop capture.** The sync adapter dispatches through `backend._loop`, the same pattern used by every other primitive. One mental model.
3. **Forward compatibility.** A future Redis-backed circuit breaker (issue #188) shares state across replicas. Switching is a backend swap, not an API change.

```python
from grelmicro import Grelmicro
from grelmicro.resilience import CircuitBreakers, CircuitBreaker
from grelmicro.resilience.circuitbreaker.memory import MemoryCircuitBreakerAdapter

micro = Grelmicro(uses=[CircuitBreakers(MemoryCircuitBreakerAdapter())])
cb = CircuitBreaker("payment")


async def async_route():
    async with cb:
        ...


def sync_route():
    with cb.from_thread:
        ...
```

## Constraints

- **The backend must be opened.** `async with backend:` (or `async with micro:` on a `Grelmicro` app) captures the loop. Without it, the sync adapter raises `AttributeError` because `backend._loop` is `None`.
- **Same loop for the lifetime of the backend.** Sync calls dispatch to the loop the backend was opened on.
- **Async is the default API.** Use `with cb.from_thread:` only inside a sync handler, to make the boundary explicit.

## Industry alignment

Per-resource loop reference is the same pattern used by `redis-py` (loop on the connection pool), `aioredis`, `httpx` sync wrapper, and SQLAlchemy `AsyncEngine`. It scales naturally to PEP 703 free-threaded Python: each backend instance carries its own loop reference, so multiple loops in the same process do not conflict.
