"""Tests for the Redis Schedule Adapter (no live server)."""

import pytest

from grelmicro.coordination.redis import RedisScheduleAdapter
from grelmicro.providers.redis import RedisProvider

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


def test_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisScheduleAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_adapter_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = RedisProvider(URL)
    backend = RedisScheduleAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_adapter_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("CACHE_REDIS_URL", URL)

    backend = RedisScheduleAdapter(env_prefix="CACHE_REDIS_")

    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "CACHE_REDIS_"


def test_provider_factory_returns_redis_adapter() -> None:
    """`RedisProvider.schedule()` returns a bound adapter."""
    provider = RedisProvider(URL)

    backend = provider.schedule()

    assert isinstance(backend, RedisScheduleAdapter)
    assert backend.provider is provider


def test_rebind_provider_borrows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    monkeypatch.setenv("REDIS_URL", URL)
    backend = RedisScheduleAdapter()
    assert backend._owns_provider is True
    other = RedisProvider(URL)

    backend._rebind_provider(other)

    assert backend.provider is other
    assert backend._owns_provider is False
