"""Test Lock Backends."""

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
from anyio import sleep
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import AsyncRedisContainer

from grelmicro.abc.lockbackend import LockBackend
from grelmicro.backends.memory import MemoryLockBackend
from grelmicro.backends.postgres import PostgresLockBackend
from grelmicro.backends.redis import RedisLockBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """AnyIO Backend."""
    return "asyncio"


@pytest.fixture(scope="module", params=["redis", "postgres", "anyio"])
async def backend(request: pytest.FixtureRequest) -> AsyncGenerator[LockBackend]:
    """Test Container for each Backend."""
    try:
        if request.param == "redis":
            with AsyncRedisContainer() as container:
                async with RedisLockBackend(
                    f"redis://@{container.get_container_host_ip()}:{container.port}/0",
                ) as backend:
                    yield backend
        elif request.param == "postgres":
            with PostgresContainer() as container:
                async with PostgresLockBackend(
                    f"postgresql://{container.username}:{container.password}@{container.get_container_host_ip()}:{container.port}/{container.dbname}",
                ) as backend:
                    yield backend
        elif request.param == "anyio":
            async with MemoryLockBackend() as backend:
                yield backend
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Failed to start container: {exc}")


async def test_acquire(backend: LockBackend) -> None:
    """Test acquire."""
    # Arrange
    name = "test_acquire"
    token = uuid4().hex
    duration = 1

    # Act
    result = await backend.acquire(name=name, token=token, duration=duration)

    # Assert
    assert result


async def test_acquire_reantrant(backend: LockBackend) -> None:
    """Test acquire is reantrant."""
    # Arrange
    name = "test_acquire_reantrant"
    token = uuid4().hex
    duration = 1

    # Act
    result1 = await backend.acquire(name=name, token=token, duration=duration)
    result2 = await backend.acquire(name=name, token=token, duration=duration)

    # Assert
    assert result1
    assert result2


async def test_acquire_already_acquired(backend: LockBackend) -> None:
    """Test acquire when already acquired."""
    # Arrange
    name = "test_acquire_already_acquired"
    token1 = uuid4().hex
    token2 = uuid4().hex
    duration = 1

    # Act
    result1 = await backend.acquire(name=name, token=token1, duration=duration)
    result2 = await backend.acquire(name=name, token=token2, duration=duration)

    # Assert
    assert token1 != token2
    assert result1
    assert not result2


async def test_acquire_expired(backend: LockBackend) -> None:
    """Test acquire when expired."""
    # Arrange
    name = "test_acquire_expired"
    token = uuid4().hex
    duration = 0.01

    # Act
    result = await backend.acquire(name=name, token=token, duration=duration)
    await sleep(duration * 2)
    result2 = await backend.acquire(name=name, token=token, duration=duration)

    # Assert
    assert result
    assert result2


async def test_acquire_already_acquired_expired(backend: LockBackend) -> None:
    """Test acquire when already acquired but expired."""
    # Arrange
    name = "test_acquire_already_acquired_expired" + uuid4().hex
    token1 = uuid4().hex
    token2 = uuid4().hex
    duration = 0.01

    # Act
    result = await backend.acquire(name=name, token=token1, duration=duration)
    await sleep(duration * 2)
    result2 = await backend.acquire(name=name, token=token2, duration=duration)

    # Assert
    assert token1 != token2
    assert result
    assert result2


async def test_release_not_acquired(backend: LockBackend) -> None:
    """Test release when not acquired."""
    # Arrange
    name = "test_release" + uuid4().hex
    token = uuid4().hex

    # Act
    result = await backend.release(name=name, token=token)

    # Assert
    assert not result


async def test_release_acquired(backend: LockBackend) -> None:
    """Test release when acquired."""
    # Arrange
    name = "test_release_acquired" + uuid4().hex
    token = uuid4().hex
    duration = 1

    # Act
    result1 = await backend.acquire(name=name, token=token, duration=duration)
    result2 = await backend.release(name=name, token=token)

    # Assert
    assert result1
    assert result2


async def test_release_not_reantrant(backend: LockBackend) -> None:
    """Test release is not reantrant."""
    # Arrange
    name = "test_release_not_reantrant" + uuid4().hex
    token = uuid4().hex
    duration = 1

    # Act
    result1 = await backend.acquire(name=name, token=token, duration=duration)
    result2 = await backend.release(name=name, token=token)
    result3 = await backend.release(name=name, token=token)

    # Assert
    assert result1
    assert result2
    assert not result3


async def test_release_acquired_expired(backend: LockBackend) -> None:
    """Test release when acquired but expired."""
    # Arrange
    name = "test_release_acquired_expired" + uuid4().hex
    token = uuid4().hex
    duration = 0.01

    # Act
    result1 = await backend.acquire(name=name, token=token, duration=duration)
    await sleep(duration * 2)
    result2 = await backend.release(name=name, token=token)

    # Assert
    assert result1
    assert not result2


async def test_release_not_acquired_expired(backend: LockBackend) -> None:
    """Test release when not acquired but expired."""
    # Arrange
    name = "test_release_not_acquired_expired" + uuid4().hex
    token = uuid4().hex
    duration = 0.01

    # Act
    result1 = await backend.acquire(name=name, token=token, duration=duration)
    await sleep(duration * 2)
    result2 = await backend.release(name=name, token=token)

    # Assert
    assert result1
    assert not result2


async def test_locked(backend: LockBackend) -> None:
    """Test locked."""
    # Arrange
    name = "test_locked" + uuid4().hex
    token = uuid4().hex
    duration = 1

    # Act
    locked_before = await backend.locked(name=name)
    await backend.acquire(name=name, token=token, duration=duration)
    locked_after = await backend.locked(name=name)

    # Assert
    assert locked_before is False
    assert locked_after is True


async def test_owned(backend: LockBackend) -> None:
    """Test owned."""
    # Arrange
    name = "test_owned" + uuid4().hex
    token = uuid4().hex
    duration = 1

    # Act
    owned_before = await backend.owned(name=name, token=token)
    await backend.acquire(name=name, token=token, duration=duration)
    owned_after = await backend.owned(name=name, token=token)

    # Assert
    assert owned_before is False
    assert owned_after is True


async def test_owned_another(backend: LockBackend) -> None:
    """Test owned another."""
    # Arrange
    name = "test_owned_another" + uuid4().hex
    token1 = uuid4().hex
    token2 = uuid4().hex
    duration = 1

    # Act
    owned_before = await backend.owned(name=name, token=token1)
    await backend.acquire(name=name, token=token1, duration=duration)
    owned_after = await backend.owned(name=name, token=token2)

    # Assert
    assert owned_before is False
    assert owned_after is False
