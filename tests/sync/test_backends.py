"""Test Backend Resigry."""

from collections.abc import Callable, Generator

import pytest

from grelmicro.sync._backends import get_lock_backend, loaded_backends
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import BackendNotLoadedError
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.postgres import PostgresSyncBackend
from grelmicro.sync.redis import RedisSyncBackend


@pytest.fixture
def clean_registry() -> Generator[None, None, None]:
    """Make sure the registry is clean."""
    loaded_backends.pop("lock", None)
    yield
    loaded_backends.pop("lock", None)


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda: MemorySyncBackend(),
        lambda: RedisSyncBackend("redis://localhost:6379/0"),
        lambda: PostgresSyncBackend(
            "postgresql://user:password@localhost:5432/db"
        ),
    ],
)
@pytest.mark.usefixtures("clean_registry")
def test_get_lock_backend(backend_factory: Callable[[], SyncBackend]) -> None:
    """Test Get Synchronization Backend."""
    # Arrange
    expected_backend = backend_factory()

    # Act
    backend = get_lock_backend()

    # Assert
    assert backend is expected_backend


@pytest.mark.usefixtures("clean_registry")
def test_get_lock_backend_not_loaded() -> None:
    """Test Get Synchronization Backend Not Loaded."""
    # Act / Assert
    with pytest.raises(BackendNotLoadedError):
        get_lock_backend()


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda: MemorySyncBackend(auto_register=False),
        lambda: RedisSyncBackend(
            "redis://localhost:6379/0", auto_register=False
        ),
        lambda: PostgresSyncBackend(
            "postgresql://user:password@localhost:5432/db", auto_register=False
        ),
    ],
)
@pytest.mark.usefixtures("clean_registry")
def test_get_lock_backend_auto_register_disabled(
    backend_factory: Callable[[], SyncBackend],
) -> None:
    """Test Get Synchronization Backend."""
    # Arrange
    backend_factory()

    # Act / Assert
    with pytest.raises(BackendNotLoadedError):
        get_lock_backend()
