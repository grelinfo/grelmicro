"""Test helpers for asserting on protocol-level interactions.

`record(backend)` instruments a backend's public async methods in place and
returns a `CallLog`. The backend keeps its real type and behavior, so it drops
into a component exactly as before, while the log captures every call for
assertions, in the spirit of `pytest-mock`'s `mocker.spy`.

```python
from grelmicro.sync.memory import MemorySyncAdapter
from grelmicro.testing import record

backend = MemorySyncAdapter()
log = record(backend)
micro = Grelmicro(uses=[Sync(backend)])

async with micro:
    await login("u1")

assert log.count("acquire", name="user:u1") == 1
```
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


@dataclass(frozen=True)
class Call:
    """One recorded protocol call."""

    method: Annotated[str, Doc("Name of the method that was called.")]
    kwargs: Annotated[
        Mapping[str, Any],
        Doc("Keyword arguments the method was called with."),
    ]


@dataclass
class CallLog:
    """Records calls made to an instrumented backend.

    Returned by `record(...)`. Exposes the raw `calls` list plus helpers to
    assert on what was called.
    """

    calls: Annotated[
        list[Call],
        Doc("Every recorded call, in order."),
    ] = field(default_factory=list)

    def count(
        self,
        method: Annotated[
            str | None,
            Doc("Method name to match, or `None` to count every call."),
        ] = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> int:
        """Return how many recorded calls match `method` and `kwargs`.

        A call matches when its method equals `method` (when given) and every
        item in `kwargs` equals the recorded keyword argument of the same name.
        """
        return sum(
            1
            for call in self.calls
            if (method is None or call.method == method)
            and all(
                call.kwargs.get(key) == value for key, value in kwargs.items()
            )
        )

    def methods(self) -> list[str]:
        """Return the method names of every recorded call, in order."""
        return [call.method for call in self.calls]

    def reset(self) -> None:
        """Drop every recorded call."""
        self.calls.clear()


def record(
    backend: Annotated[
        object,
        Doc(
            """
            The backend instance to instrument. Its public async methods are
            wrapped in place, so the same instance keeps its type and behavior.
            """,
        ),
    ],
) -> CallLog:
    """Instrument `backend`'s public async methods and return their `CallLog`.

    Each public coroutine method (one whose name does not start with `_`) is
    replaced on the instance with a wrapper that records the call and forwards
    to the original. The class and other instances are untouched.
    """
    log = CallLog()
    for name in dir(backend):
        if name.startswith("_"):
            continue
        attr = getattr(backend, name)
        if inspect.iscoroutinefunction(attr):
            setattr(backend, name, _wrap(name, attr, log))
    return log


def _wrap(
    name: str,
    method: Callable[..., Any],
    log: CallLog,
) -> Callable[..., Any]:
    """Return an async wrapper that records the call then forwards to `method`."""

    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        log.calls.append(Call(name, dict(kwargs)))
        return await method(*args, **kwargs)

    return wrapper
