# Testing

## `micro.fake()`

Swaps every backed component onto an in-process store for a block. The test
runs the real code paths against real primitives, with no Redis and no
Postgres:

```python
async def test_checkout(micro: Grelmicro) -> None:
    async with micro:
        async with micro.fake():
            await checkout("cart-1")
```

`Coordination`, `Cache`, `RateLimiterComponent`, and `CircuitBreakerComponent`
are each replaced by one wired to a fresh `MemoryProvider`, under the same
name. A `Lock("cart")` inside the block acquires against memory and behaves
like a lock, rather than like a mock that returns whatever it was told to.

Components with no backend to fake (`Log`, `Trace`, `Metrics`, `HealthChecks`)
are left alone. So is `Outbox`, which carries handlers and a running relay that
a swap would drop. Use `micro.override(...)` for those.

Reach for `fake()` when the test is about your code, and for `override()` when
the test is about the interaction with a specific backend.

## `micro.override(*components)`

Swaps components inside an active `async with micro:` block.

```python
from unittest.mock import AsyncMock

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination import LockBackend


async def test_swap_for_block(micro: Grelmicro) -> None:
    fake_backend = AsyncMock(spec=LockBackend)
    async with micro:
        async with micro.override(Coordination(lock=fake_backend)):
            await do_something_that_uses_lock()
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
from grelmicro.resilience.circuitbreaker import CircuitBreaker, CircuitBreakerState
from grelmicro.resilience.circuitbreaker.memory import MemoryCircuitBreakerAdapter


async def test_breaker_half_opens_after_cooldown() -> None:
    async with VirtualClock() as clock:
        micro = Grelmicro(uses=[MemoryCircuitBreakerAdapter()])
        async with micro:
            breaker = CircuitBreaker.consecutive_count(
                "svc", error_threshold=1, reset_timeout=30
            )
            try:
                async with breaker:
                    raise ValueError("boom")
            except ValueError:
                pass
            assert breaker.state == CircuitBreakerState.OPEN

            await clock.advance(30)  # cooldown elapses, no real wait
            async with breaker:
                pass
            assert breaker.state == CircuitBreakerState.HALF_OPEN
```

`VirtualClock` is a clock backend. Install it for the surrounding scope with `async with VirtualClock() as clock:`, then advance time by hand with `await clock.advance(seconds)`. `monotonic()` returns the virtual time and `sleep()` suspends until the clock passes its deadline.

With no clock registered, the seam forwards straight to `time.monotonic` and `asyncio.sleep`, so production keeps real time and pays only one `ContextVar` read. Only in-process backends (the memory adapters) follow the virtual clock. Redis and Postgres keep their own server-side time.

## Call recorder

`record(backend)` instruments a backend's public async methods in place and returns a `CallLog`. The backend keeps its real type and behavior, so it drops into a component exactly as before, while the log captures every protocol call for assertions. It works like `pytest-mock`'s `mocker.spy`: record without replacing.

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.testing import record


async def test_login_takes_the_lock() -> None:
    backend = MemoryLockAdapter()
    log = record(backend)
    micro = Grelmicro(uses=[Coordination(lock=backend)])

    async with micro:
        await login("u1")

    assert log.count("acquire", name="user:u1") == 1
```

`log.count(method, **kwargs)` counts calls matching a method name and keyword arguments, `log.methods()` lists the call order, and `log.reset()` clears the history. Read `log.calls` for the raw `Call` records.

## Pytest recipe

grelmicro ships no pytest plugin. Add a `micro` fixture in `conftest.py`:

```python
from collections.abc import AsyncIterator

import pytest

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter


@pytest.fixture
async def micro() -> AsyncIterator[Grelmicro]:
    app = Grelmicro(uses=[
        Coordination(lock=MemoryLockAdapter()),
        Cache(MemoryCacheAdapter()),
    ])
    async with app:
        yield app
```

Tests then read `micro` as a fixture and apply per-case overrides:

```python
from unittest.mock import AsyncMock

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination import LockBackend


async def test_login(micro: Grelmicro) -> None:
    fake_backend = AsyncMock(spec=LockBackend)
    async with micro.override(Coordination(lock=fake_backend)):
        await do_login("u1")
```

### Test the app you ship

The fixture above builds a second app for tests, so the wiring under test is
not the wiring that runs in production. `fake()` lets the fixture open the real
one and swap only the backends:

```python
from collections.abc import AsyncIterator

import pytest

from app import micro as production_micro
from grelmicro import Grelmicro


@pytest.fixture
async def micro() -> AsyncIterator[Grelmicro]:
    async with production_micro, production_micro.fake():
        yield production_micro
```

Every component, name, and default the service really registers is exercised,
against in-process stores. A component added to `app.py` and forgotten in
`conftest.py` can no longer pass the suite and fail in production.
