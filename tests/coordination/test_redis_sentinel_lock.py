"""End-to-end lock test against Redis Sentinel.

Brings up a Redis master and a Sentinel watching it on a shared Docker
network, then drives the `RedisLockAdapter` through the Sentinel-resolved
master. This proves the `redis+sentinel://` topology works with a real
adapter, not just at the URL-parsing level.

Sentinel reports the master by its Docker-network alias, which the host
cannot route. The client rewrites that to `127.0.0.1` (the master port is
published on a fixed host port), so the host reaches the master Sentinel
hands back.
"""

from collections.abc import AsyncGenerator, Generator
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.timeout(60), pytest.mark.integration]

testcontainers = pytest.importorskip("testcontainers.core.container")

from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.core.network import Network  # noqa: E402
from testcontainers.core.waiting_utils import wait_for_logs  # noqa: E402

from grelmicro.coordination.redis import RedisLockAdapter  # noqa: E402
from grelmicro.providers.redis import RedisProvider  # noqa: E402

_MASTER_PORT = 6380
_SENTINEL_PORT = 26379
_SERVICE_NAME = "mymaster"


@pytest.fixture(scope="module")
def network() -> Generator[Network]:
    """Shared Docker network for the master and the sentinel."""
    with Network() as net:
        yield net


@pytest.fixture(scope="module")
def sentinel_provider(
    network: Network,
) -> Generator[RedisProvider]:
    """Yield a `RedisProvider` resolved through a real Sentinel."""
    master = (
        DockerContainer("bitnami/redis:latest")
        .with_env("ALLOW_EMPTY_PASSWORD", "yes")
        .with_env("REDIS_PORT_NUMBER", str(_MASTER_PORT))
        .with_network(network)
        .with_network_aliases("redis-master")
        .with_bind_ports(_MASTER_PORT, _MASTER_PORT)
    )
    sentinel = (
        DockerContainer("bitnami/redis-sentinel:latest")
        .with_env("ALLOW_EMPTY_PASSWORD", "yes")
        .with_env("REDIS_MASTER_HOST", "redis-master")
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

            # Sentinel announces the master by the Docker-network alias,
            # which the host cannot route. `force_master_ip` rewrites it to
            # the loopback mapping, and the master's port (6380) is bound to
            # the same port on the host, so the resolved master is reachable.
            sentinel_client = Sentinel(
                [("localhost", _SENTINEL_PORT)],
                force_master_ip="127.0.0.1",
            )
            client = sentinel_client.master_for(_SERVICE_NAME, db=0)
            provider = RedisProvider.from_client(client, own=True)
            yield provider


@pytest.fixture
async def backend(
    sentinel_provider: RedisProvider,
) -> AsyncGenerator[RedisLockAdapter]:
    """Yield a `RedisLockAdapter` bound to the Sentinel-resolved master."""
    async with (
        sentinel_provider,
        RedisLockAdapter(provider=sentinel_provider) as backend,
    ):
        yield backend


async def test_sentinel_acquire_and_release(
    backend: RedisLockAdapter,
) -> None:
    """Acquire, re-entrant acquire, contention, and release end to end."""
    name = f"sentinel-lock-{uuid4().hex}"
    token1 = uuid4().hex
    token2 = uuid4().hex

    fence1 = await backend.acquire(name=name, token=token1, duration=5)
    assert fence1 is not None

    fence_again = await backend.acquire(name=name, token=token1, duration=5)
    assert fence_again == fence1

    fence_other = await backend.acquire(name=name, token=token2, duration=5)
    assert fence_other is None

    assert await backend.locked(name=name) is True
    assert await backend.owned(name=name, token=token1) is True

    assert await backend.release(name=name, token=token1) is True
    assert await backend.locked(name=name) is False
