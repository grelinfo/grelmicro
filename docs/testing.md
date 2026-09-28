# Testing

grelmicro gives you four tools for tests: run the app on in-process stores,
swap one backend for a test, drive time by hand, and record the calls a
pattern makes.

## Declare the test environment

Tests wire memory backends, and a memory backend behind a `Lock` is what the
[backend check](deployment.md#the-backend-check) reports. Declare the
environment once in `conftest.py` and the report stops, in the suite where it
has nothing to say:

```python
import os

os.environ.setdefault("GREL_ENVIRONMENT", "test")
```

`Grelmicro(environment="test")` does the same for one app. Keep calling
`micro.check_backends()` for the app you deploy: it answers for production
whatever this process declares.

## Test the app you ship

Enter `micro.fake()` before the test client starts. The client runs your
app's lifespan, the lifespan opens `micro`, and `micro` opens on in-process
stores:

```python
--8<-- "testing/installed_app.py"
```

The test runs the app you deploy, with every component and name it really
registers. `Coordination`, `Cache`, `RateLimiterComponent` and
`CircuitBreakerComponent` open on memory, so the `Lock` above is a real lock
that needs no database. The Postgres they would have used is never opened, so
the suite connects to nothing and `/readyz` does not probe it.

Do not open `micro` in the fixture as well. `install(app)` already opens it
when the client starts, and a second open raises.

A Provider stays open when a component that is not faked still uses it, such
as the Postgres an `Outbox` stores in. A test that reaches a closed Provider
directly, `postgres.client` for example, is told so. Pass
`micro.fake(keep=[postgres])` to open the real one for that test.

### An app with no HTTP

With no framework to open it, the fixture opens `micro` itself, after the fake:

```python
--8<-- "testing/plain_app.py"
```

## Swap one backend

`micro.override(...)` replaces a component on the open app for a block and
puts the original back on exit. The last test above uses it to check that
`reserve` takes the lock. Reach for `fake()` when the test is about your code,
and for `override()` when it is about the calls to a backend.

## Drive time with VirtualClock

Time-dependent patterns (retry backoff, circuit breaker cooldown, rate limiter
refill) read time through grelmicro's clock seam. Install a `VirtualClock` and
advance it by hand so tests never wait real seconds:

```python
from grelmicro.clock import VirtualClock


async def test_cooldown() -> None:
    async with VirtualClock() as clock:
        ...
        await clock.advance(30)  # cooldown elapses, no real wait
```

With no clock installed, the seam forwards to real time, so production pays
nothing.

## Record calls

`record(backend)` instruments a backend in place and returns a `CallLog`. The
backend keeps its real behavior while the log captures every call for assertions:

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.providers.memory import MemoryProvider
from grelmicro.testing import record


async def test_login_takes_the_lock() -> None:
    backend = MemoryProvider().lock()
    log = record(backend)
    micro = Grelmicro(uses=[Coordination(lock=backend)])

    async with micro:
        await login("u1")

    assert log.count("acquire", name="user:u1") == 1
```

## Test an authenticated app

`AuthenticatedRequests` takes any verifier that answers `verify(token)`. In a
test, hand it one that maps a token to the caller it stands for, so no key is
generated and no token is signed:

```python
--8<-- "http/authentication_testing.py"
```

A token the verifier does not know is refused the way a bad one is in
production, so the `401` and its challenge are tested too. To test the verifier
itself, generate a key pair in the test, sign a token with the private half, and
build a `JWTVerifier.keys(...)` from the public half. The
[Keys](security/jwt.md#keys) section lists the key forms it accepts.

## Going deeper

The [Testing architecture](architecture/testing.md) page covers override
restrictions, how `VirtualClock` interacts with each backend, and the full
`CallLog` API.
