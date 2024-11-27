"""Test Synchronization Backends."""

from collections.abc import AsyncGenerator, Callable, Generator
from uuid import uuid4

import pytest
from anyio import sleep
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from grelmicro.sync._backends import get_sync_backend, loaded_backends
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import BackendNotLoadedError
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.postgres import PostgresSyncBackend
from grelmicro.sync.redis import RedisSyncBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """AnyIO Backend Module Scope."""
    return "asyncio"


@pytest.fixture(scope="module")
def monkeypatch() -> Generator[pytest.MonkeyPatch, None, None]:
    """Monkeypatch Module Scope."""
    monkeypatch = pytest.MonkeyPatch()
    yield monkeypatch
    monkeypatch.undo()


@pytest.fixture
def clean_registry() -> Generator[None, None, None]:
    """Make sure the registry is clean."""
    loaded_backends.pop("lock", None)
    yield
    loaded_backends.pop("lock", None)


@pytest.fixture(
    params=[
        "memory",
        pytest.param("redis", marks=[pytest.mark.integration]),
        pytest.param("postgres", marks=[pytest.mark.integration]),
    ],
    scope="module",
)
def backend_name(request: pytest.FixtureRequest) -> str:
    """Backend Name."""
    return request.param


@pytest.fixture(
    scope="module",
)
def container(
    backend_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[DockerContainer | None, None, None]:
    """Test Container for each Backend."""
    if backend_name == "redis":
        with RedisContainer() as container:
            yield container
    elif backend_name == "postgres":
        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_PORT", "5432")
        monkeypatch.setenv("POSTGRES_DB", "test")
        monkeypatch.setenv("POSTGRES_USER", "test")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")
        with PostgresContainer() as container:
            yield container
    elif backend_name == "memory":
        yield None


@pytest.fixture(scope="module")
async def backend(
    backend_name: str, container: DockerContainer | None
) -> AsyncGenerator[SyncBackend]:
    """Test Container for each Backend."""
    if backend_name == "redis" and container:
        port = container.get_exposed_port(6379)
        async with RedisSyncBackend(f"redis://localhost:{port}/0") as backend:
            yield backend
    elif backend_name == "postgres" and container:
        port = container.get_exposed_port(5432)
        async with PostgresSyncBackend(
            "postgresql://test:test@localhost:{port}/test"
        ) as backend:
            yield backend
    elif backend_name == "memory":
        async with MemorySyncBackend() as backend:
            yield backend


async def test_acquire(backend: SyncBackend) -> None:
    """Test acquire."""
    # Arrange
    name = "test_acquire"
    token = uuid4().hex
    duration = 1

    # Act
    result = await backend.acquire(name=name, token=token, duration=duration)

    # Assert
    assert result


async def test_acquire_reantrant(backend: SyncBackend) -> None:
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


async def test_acquire_already_acquired(backend: SyncBackend) -> None:
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


async def test_acquire_expired(backend: SyncBackend) -> None:
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


async def test_acquire_already_acquired_expired(backend: SyncBackend) -> None:
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


async def test_release_not_acquired(backend: SyncBackend) -> None:
    """Test release when not acquired."""
    # Arrange
    name = "test_release" + uuid4().hex
    token = uuid4().hex

    # Act
    result = await backend.release(name=name, token=token)

    # Assert
    assert not result


async def test_release_acquired(backend: SyncBackend) -> None:
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


async def test_release_not_reantrant(backend: SyncBackend) -> None:
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


async def test_release_acquired_expired(backend: SyncBackend) -> None:
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


async def test_release_not_acquired_expired(backend: SyncBackend) -> None:
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


async def test_locked(backend: SyncBackend) -> None:
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


async def test_owned(backend: SyncBackend) -> None:
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


async def test_owned_another(backend: SyncBackend) -> None:
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
def test_get_sync_backend(backend_factory: Callable[[], SyncBackend]) -> None:
    """Test Get Synchronization Backend."""
    # Arrange
    expected_backend = backend_factory()

    # Act
    backend = get_sync_backend()

    # Assert
    assert backend is expected_backend


@pytest.mark.usefixtures("clean_registry")
def test_get_sync_backend_not_loaded() -> None:
    """Test Get Synchronization Backend Not Loaded."""
    # Act / Assert
    with pytest.raises(BackendNotLoadedError):
        get_sync_backend()


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
def test_get_sync_backend_auto_register_disabled(
    backend_factory: Callable[[], SyncBackend],
) -> None:
    """Test Get Synchronization Backend."""
    # Arrange
    backend_factory()

    # Act / Assert
    with pytest.raises(BackendNotLoadedError):
        get_sync_backend()
