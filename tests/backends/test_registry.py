"""Test Backend Resigry."""

from collections.abc import Callable, Generator

import pytest

from grelmicro.abc.lockbackend import LockBackend
from grelmicro.backends.memory.lock import MemoryLockBackend
from grelmicro.backends.postgres.lock import PostgresLockBackend
from grelmicro.backends.redis.lock import RedisLockBackend
from grelmicro.backends.registry import get_lock_backend, loaded_backends
from grelmicro.errors import BackendNotLoadedError


@pytest.fixture
def clean_registry() -> Generator[None, None, None]:
    """Make sure the registry is clean."""
    loaded_backends.pop("lock", None)
    yield
    loaded_backends.pop("lock", None)


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda: MemoryLockBackend(),
        lambda: RedisLockBackend("redis://localhost:6379/0"),
        lambda: PostgresLockBackend("postgresql://user:password@localhost:5432/db"),
    ],
)
@pytest.mark.usefixtures("clean_registry")
def test_get_lock_backend(backend_factory: Callable[[], LockBackend]) -> None:
    """Test Get Lock Backend."""
    # Arrange
    expected_backend = backend_factory()

    # Act
    backend = get_lock_backend()

    # Assert
    assert backend is expected_backend


@pytest.mark.usefixtures("clean_registry")
def test_get_lock_backend_not_loaded() -> None:
    """Test Get Lock Backend Not Loaded."""
    # Act / Assert
    with pytest.raises(BackendNotLoadedError):
        get_lock_backend()


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda: MemoryLockBackend(auto_register=False),
        lambda: RedisLockBackend("redis://localhost:6379/0", auto_register=False),
        lambda: PostgresLockBackend(
            "postgresql://user:password@localhost:5432/db", auto_register=False
        ),
    ],
)
@pytest.mark.usefixtures("clean_registry")
def test_get_lock_backend_auto_register_disabled(
    backend_factory: Callable[[], LockBackend],
) -> None:
    """Test Get Lock Backend."""
    # Arrange
    backend_factory()

    # Act / Assert
    with pytest.raises(BackendNotLoadedError):
        get_lock_backend()
