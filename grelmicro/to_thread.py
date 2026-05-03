"""Run sync code in a worker thread while keeping the parent loop reachable.

A thin wrapper around ``asyncio.to_thread`` that records the running
event loop on a contextvar so sync code in the worker thread can call
back into the loop through any grelmicro ``from_thread`` adapter
(e.g. ``Lock.from_thread``, ``CircuitBreaker.from_thread``).

Use this in place of ``asyncio.to_thread`` when the worker function
will touch a grelmicro primitive's sync adapter.

```python
from grelmicro import to_thread
from grelmicro.sync import Lock

async def main():
    lock = Lock("cart")
    await to_thread.run_sync(do_work, lock)

def do_work(lock: Lock) -> None:
    with lock.from_thread:
        ...
```
"""

from __future__ import annotations

import asyncio
from typing import Any

from grelmicro._from_thread import remember_running_loop


async def run_sync[T](
    func: object,
    /,
    *args: Any,  # noqa: ANN401
    **kwargs: Any,  # noqa: ANN401
) -> T:
    """Run ``func(*args, **kwargs)`` in a worker thread.

    Equivalent to ``asyncio.to_thread`` plus a contextvar capture so
    sync code in the worker can reach the parent loop via the
    grelmicro ``from_thread`` adapters.
    """
    remember_running_loop()
    return await asyncio.to_thread(func, *args, **kwargs)  # ty: ignore[invalid-argument-type]
