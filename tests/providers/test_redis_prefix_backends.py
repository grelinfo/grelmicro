"""Test that stores sharing a provider prefix keep out of each other's keys.

Each provider takes a unique prefix, so tests sharing a server never meet.
"""

import secrets
from collections.abc import AsyncGenerator, Generator
from datetime import timedelta

import pytest
from testcontainers.community.redis import RedisContainer

from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.valkey import ValkeyProvider
from tests._redis_cluster import CLUSTERS

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30, func_only=True)]
"""Thirty seconds of test body, with container startup left to its own wait."""


@pytest.fixture(scope="module")
def redis_url() -> Generator[str]:
    """Start one standalone Redis for the module, and return its URL."""
    with RedisContainer() as redis:
        yield f"redis://localhost:{redis.get_exposed_port(6379)}/0"


@pytest.fixture(params=["redis", *CLUSTERS])
async def provider(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[RedisProvider]:
    """Open a provider with a unique prefix on standalone or a Cluster."""
    prefix = f"{secrets.token_hex(8)}:"
    if request.param == "redis":
        provider = RedisProvider(
            request.getfixturevalue("redis_url"), prefix=prefix
        )
    elif request.param == "redis-cluster":
        provider = RedisProvider(
            request.getfixturevalue("redis_cluster_url"), prefix=prefix
        )
    else:
        provider = ValkeyProvider(
            request.getfixturevalue("valkey_cluster_url"), prefix=prefix
        )
    async with provider:
        yield provider


async def test_cache_clear_leaves_the_lock_and_its_fence(
    provider: RedisProvider,
) -> None:
    """`clear` on the cache never deletes a held lock or its fence counter."""
    # Arrange
    lock = provider.lock_backend()
    cache = provider.cache_backend()
    token = secrets.token_hex(8)
    fence = await lock.acquire(
        name="cart", token=token, duration=timedelta(seconds=30)
    )
    await cache.set(key="k", value=b"v", ttl=timedelta(seconds=30))

    # Act
    await cache.clear()

    # Assert
    client = provider.client
    kept = [
        await client.exists(lock._key("cart")),
        await client.exists(lock._fence_key("cart")),
        await cache.get(key="k"),
    ]
    assert kept == [1, 1, None]
    assert await lock.owned(name="cart", token=token) is True
    assert fence == 1
