"""Tests for Redis Rate Limiter Backend."""

import pytest

from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience.errors import ResilienceSettingsValidationError
from grelmicro.resilience.redis import RedisRateLimiterBackend

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Reset the rate limiter backend registry between tests."""
    rate_limiter_backend_registry.reset()


@pytest.mark.parametrize(
    ("environs"),
    [
        {"REDIS_URL": URL},
        {
            "REDIS_PASSWORD": "test_password",
            "REDIS_HOST": "test_host",
            "REDIS_PORT": "1234",
            "REDIS_DB": "0",
        },
    ],
)
def test_redis_env_var_settings(
    environs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Redis Settings from Environment Variables."""
    # Arrange
    for key, value in environs.items():
        monkeypatch.setenv(key, value)

    # Act
    backend = RedisRateLimiterBackend()

    # Assert
    assert backend._url == URL


@pytest.mark.parametrize(
    ("environs"),
    [
        {},
        {"REDIS_URL": "test://:test_password@test_host:1234/0"},
        {"REDIS_PASSWORD": "test_password"},
        {"REDIS_URL": URL, "REDIS_HOST": "test_host"},
        {
            "REDIS_URL": "test://:test_password@test_host:1234/0",
            "REDIS_PASSWORD": "test_password",
            "REDIS_HOST": "test_host",
            "REDIS_PORT": "1234",
            "REDIS_DB": "0",
        },
    ],
)
def test_redis_env_var_settings_validation_error(
    environs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test Redis Settings from Environment Variables."""
    # Arrange
    for key, value in environs.items():
        monkeypatch.setenv(key, value)

    # Act & Assert
    with pytest.raises(
        ResilienceSettingsValidationError,
        match=(r"Could not validate environment variables settings:\n"),
    ):
        RedisRateLimiterBackend()


def test_constructor_does_not_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing the backend performs no registry writes."""
    monkeypatch.setenv("REDIS_URL", URL)

    RedisRateLimiterBackend()

    assert rate_limiter_backend_registry.is_loaded is False


def test_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test prefix parameter is stored."""
    # Arrange
    monkeypatch.setenv("REDIS_URL", URL)

    # Act
    backend = RedisRateLimiterBackend(prefix="myapp:")

    # Assert
    assert backend._prefix == "myapp:"


def test_explicit_url() -> None:
    """Test explicit URL parameter."""
    # Act
    backend = RedisRateLimiterBackend(URL)

    # Assert
    assert backend._url == URL
