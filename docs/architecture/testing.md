# Testing

## `micro.fake()`

Runs the app on in-process stores. It takes effect wherever the app opens,
so it works for an app that `install` opens in the framework lifespan:

```python
with micro.fake(), TestClient(app) as client:
    ...
```

Entered before the app opens, it arms the next open, whoever does it: the test
client running the lifespan, or `async with micro:`. Both `with` and
`async with` arm, so a sync `TestClient` fixture and an async fixture read the
same. The arm is held on the app, not on the task, so it reaches the thread
where `TestClient` runs the lifespan.

That open replaces `Coordination`, `Cache`, `RateLimiterComponent`, and
`CircuitBreakerComponent` with components wired to a fresh `MemoryProvider`,
under the same names. A `Lock("cart")` then acquires against memory and
behaves like a lock, rather than like a mock that returns whatever it was
told to. Everything is put back when the app closes, so the next open without
a fake is the real app.

A Provider is opened only when something left as is still borrows it. One
that only the faked components used stays closed: the test connects to
nothing, and `HealthChecks(auto_health=True)` registers no readiness check for
it. Reaching it directly raises `OutOfContextError` naming the fix,
`micro.fake(keep=[provider])`, which opens the real one. The backend scope
check does not run on a faked open, since memory stores are its point.

Components with no backend to fake (`Log`, `Trace`, `Metrics`, `HealthChecks`)
are left alone. So is `Outbox`, which carries handlers and a running relay that
a swap would drop, and the Provider it borrows stays open. Use
`micro.override(...)` for those.

Entered with `async with` on an app that is already open, `fake()` swaps the
same components for the block instead, like `override()`. The Providers are
open by then, so they stay open and checked. `with` on an open app raises,
because only an async block can open the replacements.

Reach for `fake()` when the test is about your code, and for `override()` when
the test is about the interaction with a specific backend.

## `micro.override(*components)`

Swaps components inside an active `async with micro:` block.

```python
from unittest.mock import AsyncMock

from grelmicro.coordination import Coordination
from grelmicro.coordination import LockBackend

from app import micro


async def test_swap_for_block() -> None:
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

## Pytest fixtures

grelmicro ships no pytest plugin. The [testing guide](../testing.md#test-the-app-you-ship)
has the one fixture to write: enter `micro.fake()`, then let the test client,
or `async with micro:` for an app with no HTTP, open the app you ship.

A fixture that opens `micro` and then starts a client on an app that
`install` wired opens it twice, which raises `OutOfContextError` naming this
cause. To run two apps or two clients at once, build one `Grelmicro` per app,
see [app factories](multiple-apps.md#app-factories).
