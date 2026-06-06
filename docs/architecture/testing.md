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

## Call recorder

`record(backend)` instruments a backend's public async methods in place and returns a `CallLog`. The backend keeps its real type and behavior, so it drops into a component exactly as before, while the log captures every protocol call for assertions. It works like `pytest-mock`'s `mocker.spy`: record without replacing.

```python
from grelmicro import Grelmicro
from grelmicro.sync import Sync
from grelmicro.sync.memory import MemorySyncAdapter
from grelmicro.testing import record


async def test_login_takes_the_lock() -> None:
    backend = MemorySyncAdapter()
    log = record(backend)
    micro = Grelmicro(uses=[Sync(backend)])

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
