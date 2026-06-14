# Testing

grelmicro gives you three tools for tests: swap a backend for the test, drive
time by hand, and record the calls a pattern makes.

## Swap a backend

Inside an active app, `micro.override(...)` replaces a component for the duration
of a block and restores the original on exit:

```python
from unittest.mock import AsyncMock

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.abc import LockBackend


async def test_login(micro: Grelmicro) -> None:
    fake = AsyncMock(spec=LockBackend)
    async with micro:
        async with micro.override(Coordination(lock=fake)):
            await do_login("u1")
            fake.acquire.assert_awaited()
```

A handy `micro` fixture in `conftest.py` keeps tests short:

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

## Going deeper

The [Testing architecture](architecture/testing.md) page covers override
restrictions, how `VirtualClock` interacts with each backend, and the full
`CallLog` API.
