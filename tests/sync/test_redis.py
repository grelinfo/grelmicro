"""Tests for the Redis Sync Adapter."""

import pytest

from grelmicro.providers.redis import RedisProvider
from grelmicro.sync.redis import RedisSyncAdapter

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


def test_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisSyncAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_adapter_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = RedisProvider(URL)
    backend = RedisSyncAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_adapter_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("CACHE_REDIS_URL", URL)

    backend = RedisSyncAdapter(env_prefix="CACHE_REDIS_")

    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "CACHE_REDIS_"
