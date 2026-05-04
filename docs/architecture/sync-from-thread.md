# Sync from thread

grelmicro is async-first. Every primitive that does I/O (`Lock`, `TaskLock`, `LeaderElection`, `TTLCache`, `RateLimiter`) exposes an async API. To call them from a synchronous handler — typically a FastAPI sync route or a FastStream sync subscriber — grelmicro provides a sync adapter on each primitive: `lock.from_thread`, `task_lock.from_thread`, `cache.from_thread`.

## How it works

A FastAPI sync route runs in a worker thread (the framework wraps it in `asyncio.to_thread`). That thread has no running event loop, so the sync adapter cannot just `await` the async method. It schedules the coroutine on the parent loop with `asyncio.run_coroutine_threadsafe(coro, loop).result()` and blocks until the result is ready.

The "parent loop" reference is captured on the **backend** when the backend is opened during lifespan startup:

```python
async def __aenter__(self) -> Self:
    self._loop = asyncio.get_running_loop()
    return self
```

Each primitive reads the loop through its bound backend (`self.backend._loop`) when its sync adapter is invoked. No global state, no module-level helper, zero hot-path overhead on the async API.

## Usage

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

from grelmicro import lifespan
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync import Lock

backend = MemorySyncBackend()
lock = Lock("cart", backend=backend)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    async with lifespan():       # opens the backend, captures the loop
        yield


app = FastAPI(lifespan=app_lifespan)


@app.get("/sync-route")
def sync_route():               # runs in a worker thread
    with lock.from_thread:
        ...
```

## CircuitBreaker is special

`CircuitBreaker` does no I/O. It manages in-process state (counters, transitions). Its context manager works in both sync and async contexts directly — no `from_thread` adapter needed.

```python
from grelmicro.resilience import CircuitBreaker

cb = CircuitBreaker("payment")


@app.get("/async-route")
async def async_route():
    async with cb:
        ...

@app.get("/sync-route")
def sync_route():
    with cb:
        ...
```

`async with cb:` is just an alias of `with cb:`.

## Constraints

- **The backend must be opened.** `async with backend:` (or `async with grelmicro.lifespan():`) captures the loop. Without it, the sync adapter raises `AttributeError` because `backend._loop` is `None`.
- **Same loop for the lifetime of the backend.** All sync calls dispatch to the loop the backend was opened on.
- **The worker thread must be able to read `backend._loop`.** Any `threading.Thread`, `concurrent.futures.ThreadPoolExecutor`, or `asyncio.to_thread` worker can read it — the attribute lives in process memory.

## Industry alignment

Per-resource loop reference is the same pattern used by `redis-py` (loop on the connection pool), `aioredis`, `httpx` sync wrapper, and SQLAlchemy `AsyncEngine`. It scales naturally to PEP 703 free-threaded Python: each backend instance carries its own loop reference, so multiple loops in the same process do not conflict.
