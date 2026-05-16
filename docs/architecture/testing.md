# Testing

## `micro.override(*components)`

Swaps components inside an active `async with micro:` block.

```python
from grelmicro.sync import Sync


async def test_swap_for_block(micro: Grelmicro) -> None:
    async with micro:
        async with micro.override(Sync(FakeSync())):
            await do_something_that_uses_sync()
```

Override components are entered when the block opens and exited in reverse order when it closes. Prior registrations are restored on exit, including when the block raises.

### Restrictions

- Only `Component` instances can be overridden. Plain async context managers passed to `use(...)` are substituted at construction time, not through `override()`.
- Calling `micro.override(...)` outside an active `async with micro:` raises `OutOfContextError`.

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
async def test_login(micro: Grelmicro) -> None:
    async with micro.override(Sync(FakeSync())):
        await do_login("u1")
```
