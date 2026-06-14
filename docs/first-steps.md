# First Steps

The smallest grelmicro app needs one pattern and a memory backend. No extra
service, no configuration. It runs as written.

## Install

```bash
pip install grelmicro
```

See the [installation guide](installation.md) for `uv`, `poetry`, and the
backend extras.

## Your first app

Guard a shared resource with a distributed `Lock`. The memory backend keeps the
lock state in the process, so this runs with nothing else installed.

```python
--8<-- "coordination/quickstart_lock.py"
```

Three things happen here:

1. `Lock("cart")` builds a lock named `cart` with default settings.
2. `Coordination(lock=MemoryLockAdapter())` gives the lock a backend.
3. `Grelmicro(uses=[...])` wires the component into the app.

One caller holds `cart` at a time. The next caller waits for the release.

## Construct a pattern

Every pattern is built the same way. Pass the name first, then tune with keyword
arguments:

```python
from grelmicro.coordination import Lock

lock = Lock("cart", lease_duration=60)
```

Patterns with variants use factory methods:

```python
from grelmicro.resilience import RateLimiter

api = RateLimiter.sliding_window("api", limit=100, window=60)
```

Decorators take the same keyword arguments:

```python
from grelmicro.cache import cached

@cached(ttl=30)
async def get_user(user_id: int) -> dict:
    ...
```

## Next

You built a pattern and wired it into an app. Next, [wire a real app](wiring.md)
with a Redis provider and the FastAPI middleware.
