"""Tests for Redis Backends."""

import pytest

from grelmicro.sync.errors import SyncSettingsValidationError
from grelmicro.sync.redis import RedisSyncBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


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
    backend = RedisSyncBackend()

    # Assert
    assert backend._url == URL


@pytest.mark.parametrize(
    ("environs"),
    [
        {"REDIS_URL": "test://:test_password@test_host:1234/0"},
        {"REDIS_PASSWORD": "test_password"},
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

    # Assert / Act
    with pytest.raises(
        SyncSettingsValidationError,
        match=(r"Could not validate environment variables settings:\n"),
    ):
        RedisSyncBackend()
