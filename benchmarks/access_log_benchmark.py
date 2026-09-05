"""Benchmark the access log middleware.

Run with: python benchmarks/access_log_benchmark.py

Measures what one request pays for `AccessLog()`, in three shapes: a record
written, a quiet path whose record is dropped by the level, and a path named
in `exclude`. The floor for the first one is what the standard library
charges to emit a record at all, so the levers are `quiet=` and `exclude=`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grelmicro.log import AccessLogMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Scope = MutableMapping[str, Any]

ROUNDS = 20000
WARMUP = 2000


class _Null(logging.Handler):
    """Format each record and discard it, the way a sink would not."""

    def emit(self, record: logging.LogRecord) -> None:
        """Render and drop."""
        self.format(record)


async def _app(scope: Scope, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
    """Answer `200` with an empty body, as fast as an app can."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _send(message: Any) -> None:  # noqa: ANN401
    """Swallow the response."""


async def _receive() -> Any:  # noqa: ANN401
    """Answer the one message an app of ours reads."""
    return {"type": "http.request"}


def _scope(path: str) -> Scope:
    """Return a request scope shaped the way a server builds one."""
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": "",
        "scheme": "http",
        "http_version": "1.1",
        "query_string": b"page=2",
        "headers": [(b"user-agent", b"curl/8.4"), (b"host", b"service")],
        "client": ("127.0.0.1", 5000),
        "state": {},
    }


async def _measure(
    label: str,
    handler: Callable[..., Awaitable[None]],
    scope: Scope,
) -> float:
    """Return nanoseconds per request, after a warmup."""
    for _ in range(WARMUP):
        await handler(scope, _receive, _send)
    started = time.perf_counter()
    for _ in range(ROUNDS):
        await handler(scope, _receive, _send)
    per_request = (time.perf_counter() - started) / ROUNDS * 1e9
    print(f"{label:<44} {per_request:8.0f} ns/request")  # noqa: T201
    return per_request


async def main() -> None:
    """Measure the middleware in each of its three shapes."""
    access = logging.getLogger("grelmicro.access")
    access.handlers = [_Null()]
    access.propagate = False
    access.setLevel(logging.INFO)

    bare = await _measure("bare app, no middleware", _app, _scope("/orders/7"))
    written = await _measure(
        "record written", AccessLogMiddleware(_app), _scope("/orders/7")
    )
    quiet = await _measure(
        "quiet path, record dropped by the level",
        AccessLogMiddleware(_app),
        _scope("/livez"),
    )
    excluded = await _measure(
        "path named in exclude",
        AccessLogMiddleware(_app, exclude=("/orders/*",)),
        _scope("/orders/7"),
    )

    print()  # noqa: T201
    print(f"record written : {written - bare:6.0f} ns over bare")  # noqa: T201
    print(f"quiet path     : {quiet - bare:6.0f} ns over bare")  # noqa: T201
    print(f"excluded path  : {excluded - bare:6.0f} ns over bare")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
