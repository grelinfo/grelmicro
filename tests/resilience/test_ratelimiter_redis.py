"""Tests for Redis Rate Limiter Backend."""

import pytest

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience.redis import RedisRateLimiterBackend

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Reset the rate limiter backend registry between tests."""
    rate_limiter_backend_registry.reset()


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = RedisProvider(URL)

    backend = RedisRateLimiterBackend(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the backend builds its own from env vars."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisRateLimiterBackend()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_constructor_does_not_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing the backend performs no registry writes."""
    monkeypatch.setenv("REDIS_URL", URL)

    RedisRateLimiterBackend()

    assert rate_limiter_backend_registry.is_loaded is False


def test_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prefix=` is stored on the backend."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisRateLimiterBackend(prefix="myapp:")

    assert backend._prefix == "myapp:"
