# Testing

## `micro.override(*components)`

Swaps components inside an active `async with micro:` block.

```python
from unittest.mock import AsyncMock

from grelmicro import Grelmicro
from grelmicro.sync import Sync
from grelmicro.sync.abc import SyncBackend


async def test_swap_for_block(micro: Grelmicro) -> None:
    fake_backend = AsyncMock(spec=SyncBackend)
    async with micro:
        async with micro.override(Sync(fake_backend)):
            await do_something_that_uses_sync()
            fake_backend.acquire.assert_awaited()
```

Override components are entered when the block opens and exited in reverse order when it closes. Prior registrations are restored on exit, including when the block raises.

### Restrictions

- Only `Component` instances can be overridden. Plain async context managers passed to `use(...)` are substituted at construction time, not through `override()`.
- Calling `micro.override(...)` outside an active `async with micro:` raises `OutOfContextError`.

## Virtual clock

Time-dependent primitives (`Retry` backoff, `CircuitBreaker` half-open window, `RateLimiter` refill, `Shield` adaptive gate) read time through grelmicro's clock seam. Install a `VirtualClock` and advance it by hand to drive that behavior without waiting real seconds:

```python
from grelmicro import Grelmicro
from grelmicro.clock import VirtualClock
from grelmicro.resilience import CircuitBreakers
from grelmicro.resilience.circuitbreaker.memory import MemoryCircuitBreakerAdapter


async def test_breaker_half_opens_after_cooldown() -> None:
    clock = VirtualClock()
    micro = Grelmicro(uses=[clock, CircuitBreakers(MemoryCircuitBreakerAdapter())])
    async with micro:
        breaker = micro.circuitbreaker.breaker("svc", reset_timeout=30)
        await trip_the_breaker(breaker)
        await clock.advance(30)  # cooldown elapses, no real wait
        assert await admits_a_trial_call(breaker)
```

`VirtualClock` is a component: list it in `uses=` (first, so it is active before the others), then advance it through `micro.clock`. Use it directly without an app via `async with VirtualClock() as clock:`.

With no clock registered, the seam forwards straight to `time.monotonic` and `asyncio.sleep`, so production keeps real time and pays only one `ContextVar` read. Only in-process backends (the memory adapters) follow the virtual clock. Redis and Postgres keep their own server-side time.

## Pytest recipe

grelmicro ships no pytest plugin. Add a `micro` fixture in `conftest.py`:

```python
from collections.abc import AsyncIterator

import pytest

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.sync import Sync
from grelmicro.sync.memory import MemorySyncAdapter


@pytest.fixture
async def micro() -> AsyncIterator[Grelmicro]:
    app = Grelmicro(uses=[
        Sync(MemorySyncAdapter()),
        Cache(MemoryCacheAdapter()),
    ])
    async with app:
        yield app
```

Tests then read `micro` as a fixture and apply per-case overrides:

```python
from unittest.mock import AsyncMock

from grelmicro import Grelmicro
from grelmicro.sync import Sync
from grelmicro.sync.abc import SyncBackend


async def test_login(micro: Grelmicro) -> None:
    fake_backend = AsyncMock(spec=SyncBackend)
    async with micro.override(Sync(fake_backend)):
        await do_login("u1")
```
