"""Test Synchronization Backends."""

import tempfile
import time as time_module
from asyncio import sleep
from collections.abc import AsyncGenerator, Callable, Generator
from uuid import uuid4

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from grelmicro.sync import use_backend
from grelmicro.sync._backends import get_sync_backend, sync_backend_registry
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import BackendNotLoadedError
from grelmicro.sync.kubernetes import KubernetesSyncBackend
from grelmicro.sync.memory import MemorySyncBackend
from grelmicro.sync.postgres import PostgresSyncBackend
from grelmicro.sync.redis import RedisSyncBackend
from grelmicro.sync.sqlite import SQLiteSyncBackend

pytestmark = [pytest.mark.timeout(30)]


def _wait_for_k3s(
    container: DockerContainer,
    timeout: float = 60,
) -> None:
    """Wait for k3s to be ready."""
    start = time_module.time()
    while time_module.time() - start < timeout:
        exit_code, _ = container.exec("kubectl get --raw /readyz")
        if exit_code == 0:
            return
        time_module.sleep(1)
    msg = "k3s did not become ready"
    raise TimeoutError(msg)


def _extract_kubeconfig(container: DockerContainer) -> str:
    """Extract kubeconfig from k3s container."""
    exit_code, output = container.exec("cat /etc/rancher/k3s/k3s.yaml")
    if exit_code != 0:
        msg = "Failed to extract kubeconfig"
        raise RuntimeError(msg)
    return output.decode()


@pytest.fixture(scope="module")
def monkeypatch() -> Generator[pytest.MonkeyPatch, None, None]:
    """Monkeypatch Module Scope."""
    monkeypatch = pytest.MonkeyPatch()
    yield monkeypatch
    monkeypatch.undo()


@pytest.fixture
def clean_registry() -> Generator[None, None, None]:
    """Make sure the registry is clean."""
    sync_backend_registry.reset()
    yield
    sync_backend_registry.reset()


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("redis", marks=[pytest.mark.integration]),
        pytest.param("postgres", marks=[pytest.mark.integration]),
        pytest.param("kubernetes", marks=[pytest.mark.integration]),
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
    elif backend_name == "kubernetes":
        with (
            DockerContainer("rancher/k3s:v1.31.4-k3s1")
            .with_command(
                "server --disable=traefik,metrics-server --tls-san=127.0.0.1"
            )
            .with_kwargs(
                privileged=True,
                tmpfs={"/run": "", "/var/run": ""},
            )
            .with_exposed_ports(6443) as container
        ):
            _wait_for_k3s(container)
            kubeconfig_content = _extract_kubeconfig(container)
            port = container.get_exposed_port(6443)
            kubeconfig_content = kubeconfig_content.replace(
                "https://127.0.0.1:6443",
                f"https://127.0.0.1:{port}",
            )
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                f.write(kubeconfig_content)
                kubeconfig_path = f.name
            monkeypatch.setenv("KUBECONFIG", kubeconfig_path)
            monkeypatch.setenv("KUBE_NAMESPACE", "default")
            yield container
    elif backend_name in ("memory", "sqlite"):
        yield None


@pytest.fixture(scope="module")
def expire_duration(backend_name: str) -> float:
    """Lock duration for expiration tests, scaled per backend.

    SQLite and Kubernetes round the duration up to whole seconds, and the
    networked backends need enough margin to survive container-side clock
    drift; only the in-process Memory backend can use a sub-second value.
    """
    if backend_name == "memory":
        return 0.2
    return 1.0


@pytest.fixture(scope="module")
def expire_wait(backend_name: str, expire_duration: float) -> float:
    """Sleep duration to wait past lock expiration."""
    if backend_name in ("sqlite", "kubernetes"):
        return expire_duration + 1.0
    return expire_duration + 0.3


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
            f"postgresql://test:test@localhost:{port}/test"
        ) as backend:
            yield backend
    elif backend_name == "memory":
        async with MemorySyncBackend() as backend:
            yield backend
    elif backend_name == "sqlite":
        async with SQLiteSyncBackend(":memory:") as backend:
            yield backend
    elif backend_name == "kubernetes" and container:
        async with KubernetesSyncBackend(namespace="default") as backend:
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


async def test_acquire_expired(
    backend: SyncBackend, expire_duration: float, expire_wait: float
) -> None:
    """Test acquire when expired."""
    # Arrange
    name = "test_acquire_expired"
    token = uuid4().hex

    # Act
    result = await backend.acquire(
        name=name, token=token, duration=expire_duration
    )
    await sleep(expire_wait)
    result2 = await backend.acquire(
        name=name, token=token, duration=expire_duration
    )

    # Assert
    assert result
    assert result2


async def test_acquire_already_acquired_expired(
    backend: SyncBackend, expire_duration: float, expire_wait: float
) -> None:
    """Test acquire when already acquired but expired."""
    # Arrange
    name = "test_acquire_already_acquired_expired" + uuid4().hex
    token1 = uuid4().hex
    token2 = uuid4().hex

    # Act
    result = await backend.acquire(
        name=name, token=token1, duration=expire_duration
    )
    await sleep(expire_wait)
    result2 = await backend.acquire(
        name=name, token=token2, duration=expire_duration
    )

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


async def test_release_acquired_expired(
    backend: SyncBackend, expire_duration: float, expire_wait: float
) -> None:
    """Test release when acquired but expired."""
    # Arrange
    name = "test_release_acquired_expired" + uuid4().hex
    token = uuid4().hex

    # Act
    result1 = await backend.acquire(
        name=name, token=token, duration=expire_duration
    )
    await sleep(expire_wait)
    result2 = await backend.release(name=name, token=token)

    # Assert
    assert result1
    assert not result2


async def test_release_not_acquired_expired(
    backend: SyncBackend, expire_duration: float, expire_wait: float
) -> None:
    """Test release when not acquired but expired."""
    # Arrange
    name = "test_release_not_acquired_expired" + uuid4().hex
    token = uuid4().hex

    # Act
    result1 = await backend.acquire(
        name=name, token=token, duration=expire_duration
    )
    await sleep(expire_wait)
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
        MemorySyncBackend,
        lambda: RedisSyncBackend("redis://localhost:6379/0"),
        lambda: PostgresSyncBackend(
            "postgresql://user:password@localhost:5432/db"
        ),
        lambda: SQLiteSyncBackend(":memory:"),
        lambda: KubernetesSyncBackend(namespace="default"),
    ],
)
@pytest.mark.usefixtures("clean_registry")
def test_get_sync_backend(backend_factory: Callable[[], SyncBackend]) -> None:
    """`use_backend` registers the constructed backend as the default."""
    expected_backend = backend_factory()
    with pytest.warns(DeprecationWarning, match="grelmicro.sync"):
        use_backend(expected_backend)

    backend = get_sync_backend()

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
        MemorySyncBackend,
        lambda: RedisSyncBackend("redis://localhost:6379/0"),
        lambda: PostgresSyncBackend(
            "postgresql://user:password@localhost:5432/db"
        ),
        lambda: SQLiteSyncBackend(":memory:"),
        lambda: KubernetesSyncBackend(namespace="default"),
    ],
)
@pytest.mark.usefixtures("clean_registry")
def test_constructor_does_not_register(
    backend_factory: Callable[[], SyncBackend],
) -> None:
    """Constructing any sync backend performs no registry writes."""
    backend_factory()

    with pytest.raises(BackendNotLoadedError):
        get_sync_backend()
