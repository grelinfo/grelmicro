"""Chaos: Redis Sentinel master disruption and recovery.

Docs claim (RedisProvider.sentinel): "The client re-resolves the master
on failover, so wrap calls in the resilience patterns to survive the
brief window where in-flight commands can error."

This proves that story end to end:

1. A lock works through a Sentinel-resolved master.
2. The master is disrupted (paused). An in-flight lock op errors.
3. A `retrying(...)` block around the lock op recovers once the master
   is back, exactly as the docs prescribe.

Honesty note: the shared sentinel harness has ONE master and NO replica,
so a real promotion (Sentinel electing a replica) cannot happen. A
single-master setup can only recover the same master. We therefore do a
master pause/unpause (a master restart in effect) rather than a true
replica promotion, and assert the documented "in-flight errors, retry
recovers" behavior. This is the strongest claim a one-master topology
can support, and it is called out as such.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(120),
]

testcontainers = pytest.importorskip("testcontainers.core.container")

from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.core.network import Network  # noqa: E402
from testcontainers.core.waiting_utils import wait_for_logs  # noqa: E402

from grelmicro.coordination.errors import LockAcquireError  # noqa: E402
from grelmicro.coordination.lock import Lock  # noqa: E402
from grelmicro.coordination.redis import RedisLockAdapter  # noqa: E402
from grelmicro.providers.redis import RedisProvider  # noqa: E402
from grelmicro.resilience import retrying  # noqa: E402
from grelmicro.resilience.backoffs import ConstantBackoff  # noqa: E402

from .conftest import wait_until  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

_MASTER_PORT = 6381
_SENTINEL_PORT = 26380
_SERVICE_NAME = "mymaster"


@pytest.fixture(scope="module")
def network() -> Generator[Network]:
    """Shared Docker network for the master and the sentinel."""
    with Network() as net:
        yield net


@pytest.fixture(scope="module")
def sentinel_topology(
    network: Network,
) -> Generator[tuple[RedisProvider, DockerContainer]]:
    """Yield ``(provider, master_container)`` resolved through a Sentinel."""
    master = (
        DockerContainer(
            "bitnami/redis@sha256:4f041bfd31d9be9711b3dc1300b4c1f90dca58c13216c84aa47fa4898945e491"
        )
        .with_env("ALLOW_EMPTY_PASSWORD", "yes")
        .with_env("REDIS_PORT_NUMBER", str(_MASTER_PORT))
        .with_network(network)
        .with_network_aliases("redis-master-chaos")
        .with_bind_ports(_MASTER_PORT, _MASTER_PORT)
    )
    sentinel = (
        DockerContainer(
            "bitnami/redis-sentinel@sha256:eaf106336a4e8b1eb5c11efa9cf5315387c42129669c88f3653351617e703229"
        )
        .with_env("ALLOW_EMPTY_PASSWORD", "yes")
        .with_env("REDIS_MASTER_HOST", "redis-master-chaos")
        .with_env("REDIS_MASTER_PORT_NUMBER", str(_MASTER_PORT))
        .with_env("REDIS_MASTER_SET", _SERVICE_NAME)
        .with_env("REDIS_SENTINEL_QUORUM", "1")
        .with_env("REDIS_SENTINEL_PORT_NUMBER", str(_SENTINEL_PORT))
        .with_network(network)
        .with_bind_ports(_SENTINEL_PORT, _SENTINEL_PORT)
    )
    with master:
        wait_for_logs(master, "Ready to accept connections", timeout=30)
        with sentinel:
            wait_for_logs(sentinel, "Sentinel new configuration", timeout=30)
            from redis.asyncio.sentinel import Sentinel  # noqa: PLC0415

            sentinel_client = Sentinel(
                [("localhost", _SENTINEL_PORT)],
                force_master_ip="127.0.0.1",
                socket_timeout=1.0,
                socket_connect_timeout=1.0,
            )
            client = sentinel_client.master_for(
                _SERVICE_NAME,
                db=0,
                socket_timeout=1.0,
                socket_connect_timeout=1.0,
            )
            provider = RedisProvider.from_client(client, own=True)
            yield provider, master


@pytest.fixture
async def lock_backend(
    sentinel_topology: tuple[RedisProvider, DockerContainer],
) -> AsyncGenerator[RedisLockAdapter]:
    """Yield a `RedisLockAdapter` bound to the Sentinel-resolved master."""
    provider, _ = sentinel_topology
    async with RedisLockAdapter(provider=provider) as backend:
        yield backend


async def test_master_disruption_in_flight_errors_retry_recovers(
    sentinel_topology: tuple[RedisProvider, DockerContainer],
    lock_backend: RedisLockAdapter,
) -> None:
    """In-flight commands error on master loss, a Retry block recovers.

    Master pause/unpause stands in for a single-master "restart" (no
    replica to promote). The documented contract is: wrap the call in a
    resilience pattern and it survives the disruption window.
    """
    _, master = sentinel_topology
    name = f"sentinel-chaos-{uuid4().hex}"

    # 1. Healthy: a plain acquire/release works through the master.
    lock = Lock(name, backend=lock_backend, lease_duration=10)
    handle = await lock.acquire_nowait()
    assert handle.fencing_token > 0
    await lock.release()

    wrapped = master.get_wrapped_container()

    # 2. Disrupt the master. An in-flight op errors loudly.
    wrapped.pause()
    try:
        disrupted = Lock(
            f"{name}-disrupted", backend=lock_backend, lease_duration=10
        )
        with pytest.raises(LockAcquireError):
            await disrupted.acquire_nowait()
    finally:
        # 3. Recovery window: bring the master back and let the
        # resilience pattern carry the call through.
        wrapped.unpause()

    # The documented answer: wrap in a Retry. The backoff spans the
    # reconnect window so the lock op eventually succeeds.
    recovered = Lock(
        f"{name}-recovered", backend=lock_backend, lease_duration=10
    )
    backoff = ConstantBackoff(delay=0.5)

    fencing = 0
    async for attempt in retrying(
        when=LockAcquireError, attempts=30, backoff=backoff
    ):
        async with attempt:
            held = await recovered.acquire_nowait()
            fencing = held.fencing_token
            await recovered.release()
    assert fencing > 0, (
        "a Retry-wrapped lock op must recover after the master is back"
    )

    # Sanity: the master is fully serving plain ops again.
    async def plain_works() -> bool:
        probe = Lock(
            f"{name}-final-{uuid4().hex}",
            backend=lock_backend,
            lease_duration=5,
        )
        h = await probe.acquire_nowait()
        await probe.release()
        return h.fencing_token > 0

    assert await wait_until(plain_works, timeout=15)
