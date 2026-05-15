"""Tests for Redis Rate Limiter Backend."""

import pytest

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience.redis import RedisRateLimiterAdapter

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = RedisProvider(URL)

    backend = RedisRateLimiterAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the backend builds its own from env vars."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisRateLimiterAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prefix=` is stored on the backend."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisRateLimiterAdapter(prefix="myapp:")

    assert backend._prefix == "myapp:"
