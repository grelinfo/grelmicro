"""Chaos: Lock loud failure under real Redis death.

Docs claim: the distributed lock fails loudly. `acquire`, `release`,
and `extend` raise the documented `Lock*Error` on a backend error
rather than hanging or silently succeeding.

We hold a lock on a real Redis, pause Redis, and assert each operation
raises the documented error in bounded time. The failure-surface
latency is measured and reported.

Bounding note: `Lock` has no `backend_timeout`. The latency is bounded
only by the Redis client's `socket_timeout`. The grelmicro
`RedisProvider` default opens a client with NO socket timeout, so a
production lock against a frozen Redis would block until the OS TCP
timeout. This test injects a 1s `socket_timeout` to make the failure
deterministic and is flagged in the report as a docs caveat.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from grelmicro.coordination.errors import (
    LockAcquireError,
    LockReleaseError,
)
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.redis import RedisLockAdapter
from grelmicro.providers.redis import RedisProvider

from .conftest import build_client, paused, wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from testcontainers.redis import RedisContainer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(60),
]

# Latency ceiling for a surfaced backend error. The client socket_timeout
# is 1.0s; allow margin for scheduling but assert it does NOT hang.
MAX_FAILURE_LATENCY = 4.0


@pytest.fixture
async def lock_backend(
    redis_container: RedisContainer,
) -> AsyncGenerator[RedisLockAdapter]:
    """Yield a Redis lock adapter on a bounded-timeout client."""
    client = build_client(redis_container)
    provider = RedisProvider.from_client(client, own=True)
    async with provider, RedisLockAdapter(provider=provider) as adapter:
        yield adapter


async def test_lock_operations_fail_loudly_when_redis_dies(
    redis_container: RedisContainer,
    lock_backend: RedisLockAdapter,
) -> None:
    """Acquire / extend / release raise documented Lock errors, bounded.

    A held lock loses its backend. Every subsequent lock operation must
    raise a `Lock*Error` (loud failure), and must surface in bounded
    time rather than hang.
    """
    lock = Lock(
        f"chaos-lock-{uuid4().hex}",
        backend=lock_backend,
        lease_duration=30,
    )

    handle = await lock.acquire()
    assert handle.fencing_token > 0

    latencies: dict[str, float] = {}

    with paused(redis_container):
        # extend: backend error -> LockAcquireError (extend calls
        # do_acquire under the hood, which wraps backend errors).
        t0 = time.perf_counter()
        with pytest.raises(LockAcquireError):
            await lock.extend()
        latencies["extend"] = time.perf_counter() - t0

        # release: backend error -> LockReleaseError.
        t0 = time.perf_counter()
        with pytest.raises(LockReleaseError):
            await lock.release()
        latencies["release"] = time.perf_counter() - t0

        # A fresh acquire (no-wait) on a frozen backend -> LockAcquireError.
        fresh = Lock(
            f"chaos-lock2-{uuid4().hex}",
            backend=lock_backend,
            lease_duration=30,
        )
        t0 = time.perf_counter()
        with pytest.raises(LockAcquireError):
            await fresh.acquire_nowait()
        latencies["acquire_nowait"] = time.perf_counter() - t0

    # None of them hung: all surfaced within the socket-timeout ceiling.
    for op, latency in latencies.items():
        assert latency < MAX_FAILURE_LATENCY, (
            f"{op} took {latency:.3f}s, expected a bounded loud failure"
        )

    # Report the measured failure-surface latencies.
    log = logging.getLogger(__name__)
    log.info("[chaos] Lock failure-surface latency (socket_timeout=1.0s):")
    for op, latency in latencies.items():
        log.info("  %s  %.1f ms", op, latency * 1000)

    # Recovery: a brand-new lock acquires cleanly once Redis is back.
    async def can_acquire() -> bool:
        probe = Lock(
            f"recover-{uuid4().hex}",
            backend=lock_backend,
            lease_duration=5,
        )
        h = await probe.acquire_nowait()
        await probe.release()
        return h.fencing_token > 0

    assert await wait_until(can_acquire, timeout=15), (
        "lock must work again once Redis recovers"
    )
