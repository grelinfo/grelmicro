"""Shared chaos-test fixtures and fault-injection helpers.

The fault we inject is ``docker pause`` / ``docker unpause`` on the
backend container. Pausing freezes the process while keeping the TCP
listener and the published host port intact, so:

* in-flight and new commands block until the client's ``socket_timeout``
  fires, surfacing a real backend error in bounded time, and
* ``unpause`` restores the exact same host port, so the same client URL
  recovers without rebuilding the connection.

That makes both the failure and the recovery deterministic, which a
``stop`` / ``start`` cycle does not give us (``stop`` drops the port
mapping and ``start`` reassigns a fresh host port).

Every Redis client here is built with a short ``socket_timeout`` so a
paused backend surfaces as a `TimeoutError` quickly. The grelmicro
`RedisProvider` default opens a client with no socket timeout, so a
production deployment that wants bounded failure latency must set one.
This is noted in the report as a docs caveat.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

testcontainers = pytest.importorskip("testcontainers.redis")

from testcontainers.redis import RedisContainer  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator

    from redis.asyncio import Redis

# Short socket timeout so a paused backend surfaces a TimeoutError fast
# and the failure-surface latency is bounded and measurable.
SOCKET_TIMEOUT = 1.0


@contextmanager
def paused(container: RedisContainer) -> Generator[None]:
    """Pause the container for the duration of the block, then unpause.

    Freezes the backend process (SIGSTOP via the cgroup freezer) while
    keeping the published host port, so commands time out instead of
    getting a connection refusal, and recovery reuses the same port.
    """
    wrapped = container.get_wrapped_container()
    wrapped.pause()
    try:
        yield
    finally:
        wrapped.unpause()


def redis_url(container: RedisContainer, db: int = 0) -> str:
    """Return a host-reachable ``redis://`` URL for the container."""
    port = container.get_exposed_port(6379)
    return f"redis://localhost:{port}/{db}"


def build_client(container: RedisContainer, db: int = 0) -> Redis:
    """Build an async Redis client with a bounded socket timeout."""
    from redis.asyncio import Redis  # noqa: PLC0415

    return Redis.from_url(
        redis_url(container, db),
        socket_timeout=SOCKET_TIMEOUT,
        socket_connect_timeout=SOCKET_TIMEOUT,
    )


async def wait_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 10.0,  # noqa: ASYNC109 - manual poll loop, not an asyncio timeout
    interval: float = 0.1,
) -> bool:
    """Poll ``predicate`` (async, returns bool) until true or deadline.

    Returns True on success, False if the deadline elapsed. Deterministic
    by deadline, never a bare sleep hoping for timing.
    """
    import anyio  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if await predicate():
                return True
        except Exception:  # noqa: BLE001, S110 - backend still down, keep polling
            pass
        await anyio.sleep(interval)
    return False


@pytest.fixture(scope="module")
def redis_container() -> Generator[RedisContainer]:
    """Return a module-scoped Redis container reused across the module's tests.

    The pause/unpause fault is fully reversed by each test, so a single
    container is safe to share and keeps the suite under the time budget.
    """
    with RedisContainer() as container:
        yield container
