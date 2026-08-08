"""Test Read-Write Lock Backends.

One conformance suite every backend must pass. The invariants are the same
everywhere: a writer is never concurrent with a reader, the generation only
grows, a waiting writer keeps new readers out, and an expired holder never
blocks anyone.
"""

from asyncio import sleep
from collections.abc import AsyncGenerator, Generator
from uuid import uuid4

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from grelmicro.coordination._protocol import ReadWriteLockBackend
from grelmicro.coordination.kubernetes import KubernetesReadWriteLockAdapter
from grelmicro.coordination.memory import MemoryReadWriteLockAdapter
from grelmicro.coordination.postgres import PostgresReadWriteLockAdapter
from grelmicro.coordination.redis import RedisReadWriteLockAdapter
from grelmicro.coordination.sqlite import SQLiteReadWriteLockAdapter
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider

pytestmark = [pytest.mark.timeout(30, func_only=True)]

_READERS = 3


@pytest.fixture(scope="module")
def monkeypatch() -> Generator[pytest.MonkeyPatch, None, None]:
    """Monkeypatch Module Scope."""
    monkeypatch = pytest.MonkeyPatch()
    yield monkeypatch
    monkeypatch.undo()


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("redis", marks=[pytest.mark.integration]),
        pytest.param("postgres", marks=[pytest.mark.integration]),
        pytest.param(
            "kubernetes",
            marks=[
                pytest.mark.integration,
                pytest.mark.xdist_group("k3s"),
            ],
        ),
    ],
    scope="module",
)
def backend_name(request: pytest.FixtureRequest) -> str:
    """Backend Name."""
    return request.param


@pytest.fixture(scope="module")
def container(
    backend_name: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
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
        monkeypatch.setenv(
            "KUBECONFIG", request.getfixturevalue("k3s_kubeconfig")
        )
        monkeypatch.setenv("KUBE_NAMESPACE", "default")
        yield None
    elif backend_name in ("memory", "sqlite"):
        yield None


@pytest.fixture(scope="module")
def expire_duration(backend_name: str) -> float:
    """Lease duration for expiration tests, scaled per backend.

    SQLite and Kubernetes round the duration up to whole seconds, and the
    networked backends need enough margin to survive container-side clock
    drift. Only the in-process Memory backend can use a sub-second value.
    """
    if backend_name == "memory":
        return 0.2
    return 1.0


@pytest.fixture(scope="module")
def expire_wait(backend_name: str, expire_duration: float) -> float:
    """Sleep duration to wait past lease expiration."""
    if backend_name in ("sqlite", "kubernetes"):
        return expire_duration + 1.0
    return expire_duration + 0.3


@pytest.fixture(scope="module")
async def backend(
    backend_name: str, container: DockerContainer | None
) -> AsyncGenerator[ReadWriteLockBackend]:
    """Read-write lock backend under test."""
    if backend_name == "redis" and container:
        port = container.get_exposed_port(6379)
        provider = RedisProvider(f"redis://localhost:{port}/0")
        async with RedisReadWriteLockAdapter(provider=provider) as backend:
            yield backend
    elif backend_name == "postgres" and container:
        port = container.get_exposed_port(5432)
        provider = PostgresProvider(
            f"postgresql://test:test@localhost:{port}/test"
        )
        async with (
            provider,
            PostgresReadWriteLockAdapter(provider=provider) as backend,
        ):
            yield backend
    elif backend_name == "memory":
        async with MemoryReadWriteLockAdapter() as backend:
            yield backend
    elif backend_name == "sqlite":
        provider = SQLiteProvider(":memory:")
        async with (
            provider,
            SQLiteReadWriteLockAdapter(provider=provider) as backend,
        ):
            yield backend
    elif backend_name == "kubernetes":
        # `container` points the adapter at the shared k3s server through
        # the environment, so it is a dependency even though it yields None.
        async with KubernetesReadWriteLockAdapter(
            namespace="default"
        ) as backend:
            yield backend


async def test_readers_share(backend: ReadWriteLockBackend) -> None:
    """Several readers hold the lock at the same time."""
    name = "test_readers_share"
    tokens = [uuid4().hex for _ in range(_READERS)]

    granted = [
        await backend.acquire_read(name=name, token=token, duration=10)
        for token in tokens
    ]

    assert all(generation is not None for generation in granted)
    state = await backend.state(name=name)
    assert state.readers == _READERS
    assert not state.writing


async def test_writer_excludes_readers(backend: ReadWriteLockBackend) -> None:
    """A live writer keeps readers out."""
    name = "test_writer_excludes_readers"
    writer, reader = uuid4().hex, uuid4().hex

    grant = await backend.acquire_write(name=name, token=writer, duration=10)
    refused = await backend.acquire_read(name=name, token=reader, duration=10)

    assert grant is not None
    assert refused is None


async def test_readers_exclude_writer(backend: ReadWriteLockBackend) -> None:
    """A reader keeps a writer out, and the writer records an intent."""
    name = "test_readers_exclude_writer"
    reader, writer = uuid4().hex, uuid4().hex

    await backend.acquire_read(name=name, token=reader, duration=10)
    refused = await backend.acquire_write(name=name, token=writer, duration=10)

    assert refused is None
    assert (await backend.state(name=name)).waiting_writers == 1


async def test_waiting_writer_blocks_new_readers(
    backend: ReadWriteLockBackend,
) -> None:
    """A recorded intent keeps new readers out while old readers finish."""
    name = "test_waiting_writer_blocks_new_readers"
    reader, writer, latecomer = uuid4().hex, uuid4().hex, uuid4().hex

    await backend.acquire_read(name=name, token=reader, duration=10)
    await backend.acquire_write(name=name, token=writer, duration=10)

    assert (
        await backend.acquire_read(name=name, token=latecomer, duration=10)
        is None
    )
    assert (
        await backend.acquire_read(name=name, token=reader, duration=10)
        is not None
    )


async def test_writer_enters_after_last_reader_leaves(
    backend: ReadWriteLockBackend,
) -> None:
    """The waiting writer is granted once the last reader releases."""
    name = "test_writer_enters_after_last_reader_leaves"
    reader, writer = uuid4().hex, uuid4().hex

    await backend.acquire_read(name=name, token=reader, duration=10)
    await backend.acquire_write(name=name, token=writer, duration=10)
    assert await backend.release_read(name=name, token=reader)

    grant = await backend.acquire_write(name=name, token=writer, duration=10)

    assert grant is not None
    assert not grant.poisoned
    assert (await backend.state(name=name)).waiting_writers == 0


async def test_nowait_writer_records_no_intent(
    backend: ReadWriteLockBackend,
) -> None:
    """A try that does not wait leaves readers free to arrive."""
    name = "test_nowait_writer_records_no_intent"
    reader, writer, latecomer = uuid4().hex, uuid4().hex, uuid4().hex

    await backend.acquire_read(name=name, token=reader, duration=10)
    refused = await backend.acquire_write(
        name=name, token=writer, duration=10, intent=False
    )

    assert refused is None
    assert (await backend.state(name=name)).waiting_writers == 0
    assert (
        await backend.acquire_read(name=name, token=latecomer, duration=10)
        is not None
    )


async def test_cancel_intent_lets_readers_in(
    backend: ReadWriteLockBackend,
) -> None:
    """A writer that stops waiting stops holding readers out."""
    name = "test_cancel_intent_lets_readers_in"
    reader, writer, latecomer = uuid4().hex, uuid4().hex, uuid4().hex

    await backend.acquire_read(name=name, token=reader, duration=10)
    await backend.acquire_write(name=name, token=writer, duration=10)
    assert await backend.cancel_intent(name=name, token=writer)
    assert not await backend.cancel_intent(name=name, token=writer)

    assert (
        await backend.acquire_read(name=name, token=latecomer, duration=10)
        is not None
    )


async def test_generation_grows_per_write(
    backend: ReadWriteLockBackend,
) -> None:
    """Each write acquisition mints a strictly greater fencing token."""
    name = "test_generation_grows_per_write"
    first, second = uuid4().hex, uuid4().hex

    grant_first = await backend.acquire_write(
        name=name, token=first, duration=10
    )
    assert grant_first is not None
    await backend.release_write(name=name, token=first)
    grant_second = await backend.acquire_write(
        name=name, token=second, duration=10
    )

    assert grant_second is not None
    assert grant_second.fencing_token > grant_first.fencing_token


async def test_same_writer_extend_keeps_generation(
    backend: ReadWriteLockBackend,
) -> None:
    """A renewal by the holder keeps its fencing token."""
    name = "test_same_writer_extend_keeps_generation"
    writer = uuid4().hex

    first = await backend.acquire_write(name=name, token=writer, duration=10)
    second = await backend.acquire_write(name=name, token=writer, duration=10)

    assert first is not None
    assert second is not None
    assert first.fencing_token == second.fencing_token


async def test_reader_sees_write_generation(
    backend: ReadWriteLockBackend,
) -> None:
    """A read taken after a write observes that write's generation."""
    name = "test_reader_sees_write_generation"
    writer, reader = uuid4().hex, uuid4().hex

    grant = await backend.acquire_write(name=name, token=writer, duration=10)
    assert grant is not None
    await backend.release_write(name=name, token=writer)
    generation = await backend.acquire_read(
        name=name, token=reader, duration=10
    )

    assert generation == grant.fencing_token


async def test_expired_reader_does_not_block_writer(
    backend: ReadWriteLockBackend,
    expire_duration: float,
    expire_wait: float,
) -> None:
    """A reader that died is reaped by the writer's own acquire."""
    name = "test_expired_reader_does_not_block_writer"
    reader, writer = uuid4().hex, uuid4().hex

    await backend.acquire_read(
        name=name, token=reader, duration=expire_duration
    )
    await sleep(expire_wait)
    grant = await backend.acquire_write(name=name, token=writer, duration=10)

    assert grant is not None


async def test_expired_writer_poisons_the_next(
    backend: ReadWriteLockBackend,
    expire_duration: float,
    expire_wait: float,
) -> None:
    """A writer that died without releasing is reported to its successor."""
    name = "test_expired_writer_poisons_the_next"
    dead, next_writer = uuid4().hex, uuid4().hex

    await backend.acquire_write(name=name, token=dead, duration=expire_duration)
    await sleep(expire_wait)
    grant = await backend.acquire_write(
        name=name, token=next_writer, duration=10
    )

    assert grant is not None
    assert grant.poisoned


async def test_clean_release_does_not_poison(
    backend: ReadWriteLockBackend,
) -> None:
    """A writer that released cleanly leaves no poison behind."""
    name = "test_clean_release_does_not_poison"
    first, second = uuid4().hex, uuid4().hex

    await backend.acquire_write(name=name, token=first, duration=10)
    assert await backend.release_write(name=name, token=first)
    grant = await backend.acquire_write(name=name, token=second, duration=10)

    assert grant is not None
    assert not grant.poisoned


async def test_downgrade_hands_no_gap_to_another_writer(
    backend: ReadWriteLockBackend,
) -> None:
    """A downgrade leaves the caller reading and keeps writers out."""
    name = "test_downgrade_hands_no_gap"
    writer, other = uuid4().hex, uuid4().hex

    grant = await backend.acquire_write(name=name, token=writer, duration=10)
    assert grant is not None
    generation = await backend.downgrade(name=name, token=writer, duration=10)

    assert generation == grant.fencing_token
    assert await backend.owned_read(name=name, token=writer)
    assert not await backend.owned_write(name=name, token=writer)
    assert (
        await backend.acquire_write(
            name=name, token=other, duration=10, intent=False
        )
        is None
    )


async def test_downgrade_without_the_lock(
    backend: ReadWriteLockBackend,
) -> None:
    """A caller that does not hold the write lock cannot downgrade."""
    name = "test_downgrade_without_the_lock"

    assert (
        await backend.downgrade(name=name, token=uuid4().hex, duration=10)
        is None
    )


async def test_release_reports_what_it_did(
    backend: ReadWriteLockBackend,
) -> None:
    """Releasing a lease that is not held reports `False`."""
    name = "test_release_reports_what_it_did"
    token = uuid4().hex

    assert not await backend.release_read(name=name, token=token)
    assert not await backend.release_write(name=name, token=token)
    await backend.acquire_read(name=name, token=token, duration=10)
    assert await backend.release_read(name=name, token=token)


async def test_owned_and_state_on_a_fresh_name(
    backend: ReadWriteLockBackend,
) -> None:
    """A name nobody touched reads as free."""
    name = f"test_fresh_{uuid4().hex}"

    state = await backend.state(name=name)

    assert state == type(state)(
        generation=0, writing=False, readers=0, waiting_writers=0
    )
    assert not await backend.owned_read(name=name, token="nobody")
    assert not await backend.owned_write(name=name, token="nobody")


async def test_owned_tracks_the_holder(backend: ReadWriteLockBackend) -> None:
    """`owned_read` and `owned_write` follow the live holder."""
    name = "test_owned_tracks_the_holder"
    reader, writer = uuid4().hex, uuid4().hex

    await backend.acquire_read(name=name, token=reader, duration=10)
    assert await backend.owned_read(name=name, token=reader)
    assert not await backend.owned_write(name=name, token=reader)
    await backend.release_read(name=name, token=reader)

    await backend.acquire_write(name=name, token=writer, duration=10)
    assert await backend.owned_write(name=name, token=writer)
    assert not await backend.owned_read(name=name, token=writer)
