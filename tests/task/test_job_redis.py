"""Test what only the Redis job backend promises."""

import secrets
from collections.abc import AsyncGenerator, Generator
from datetime import timedelta
from types import TracebackType
from typing import Self
from unittest.mock import MagicMock

import pytest
from redis.crc import key_slot
from testcontainers.community.redis import RedisContainer

from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.task import JobStatus
from grelmicro.task._protocol import Beat
from grelmicro.task.redis import RedisJobAdapter, _record

pytestmark = [pytest.mark.timeout(30, func_only=True)]

URL = "redis://test_host:1234/0"
NAME = "app:job"


def test_job_redis_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    # Arrange
    monkeypatch.setenv("REDIS_URL", URL)

    # Act
    backend = RedisJobAdapter()

    # Assert
    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_job_redis_adapter_borrows_an_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    # Arrange
    provider = RedisProvider(URL)

    # Act
    backend = RedisJobAdapter(provider=provider)

    # Assert
    assert backend.provider is provider
    assert backend._owns_provider is False


def test_job_redis_adapter_env_prefix_reaches_the_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    # Arrange
    monkeypatch.setenv("JOBS_REDIS_URL", URL)

    # Act
    backend = RedisJobAdapter(env_prefix="JOBS_REDIS_")

    # Assert
    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "JOBS_REDIS_"


def test_job_redis_provider_factory_returns_the_adapter() -> None:
    """`RedisProvider.job_backend()` returns a bound adapter."""
    # Arrange
    provider = RedisProvider(URL)

    # Act
    backend = provider.job_backend(prefix="jobs:")

    # Assert
    assert isinstance(backend, RedisJobAdapter)
    assert backend.provider is provider
    assert backend._key_prefix == "jobs:"


def test_job_redis_valkey_provider_ships_no_job_adapter() -> None:
    """`ValkeyProvider.job_backend()` raises like any provider without one."""
    # Arrange
    provider = ValkeyProvider(URL)

    # Act & Assert
    with pytest.raises(NotImplementedError, match="ValkeyProvider has no job"):
        provider.job_backend()


def test_job_redis_rebind_provider_borrows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    # Arrange
    monkeypatch.setenv("REDIS_URL", URL)
    backend = RedisJobAdapter()
    other = RedisProvider(URL)

    # Act
    backend._rebind_provider(other)

    # Assert
    assert backend.provider is other
    assert backend._owns_provider is False


def test_job_redis_cluster_prefix_without_hash_tag_is_refused() -> None:
    """On a Redis Cluster, a prefix with no hash tag is refused."""
    # Arrange
    provider = RedisProvider("redis+cluster://h1:6379,h2:6379")

    # Act & Assert
    with pytest.raises(ValueError, match="hash tag"):
        RedisJobAdapter(provider=provider, prefix="jobs:")


def test_job_redis_cluster_prefix_with_hash_tag_is_accepted() -> None:
    """On a Redis Cluster, a prefix with a hash tag is accepted."""
    # Arrange
    provider = RedisProvider("redis+cluster://h1:6379,h2:6379")

    # Act
    backend = RedisJobAdapter(provider=provider, prefix="{app}")

    # Assert
    assert backend.provider is provider


def test_job_redis_every_key_shares_the_prefix_slot() -> None:
    """Every key of the store hashes to the slot of the prefix's tag."""
    # Arrange
    backend = RedisJobAdapter(provider=RedisProvider(URL), prefix="{app}")

    # Act
    keys = [
        *backend._keys("{odd}name", "{odd}id"),
        f"{backend._base}order:{{odd}}name",
    ]

    # Assert
    assert {key_slot(key.encode()) for key in keys} == {key_slot(b"app")}


class _StubProvider:
    """Minimal `RedisProvider`-shaped stub tracking enter and exit calls."""

    def __init__(self) -> None:
        self.client = MagicMock()
        self.is_cluster = False
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> Self:
        self.enter_count += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_count += 1


async def test_job_redis_owned_provider_is_opened_and_closed() -> None:
    """When owned, the adapter opens and closes its provider."""
    # Arrange
    stub = _StubProvider()
    backend = RedisJobAdapter(provider=stub)  # ty: ignore[invalid-argument-type]
    backend._owns_provider = True

    # Act
    async with backend:
        pass

    # Assert
    assert (stub.enter_count, stub.exit_count) == (1, 1)


async def test_job_redis_borrowed_provider_is_left_alone() -> None:
    """An external provider is not entered or exited by the adapter."""
    # Arrange
    stub = _StubProvider()
    backend = RedisJobAdapter(provider=stub)  # ty: ignore[invalid-argument-type]

    # Act
    async with backend:
        pass

    # Assert
    assert (stub.enter_count, stub.exit_count) == (0, 0)


async def test_job_redis_calls_with_nothing_to_do_send_nothing() -> None:
    """A claim with no free slot, an empty beat and a purge reach no server."""
    # Arrange
    stub = _StubProvider()
    backend = RedisJobAdapter(provider=stub)  # ty: ignore[invalid-argument-type]
    lease = timedelta(seconds=30)

    # Act
    claimed = await backend.claim(slots={NAME: 0}, runner="r", lease=lease)
    replies = await backend.beat(runner="r", beats=[], lease=lease)
    purged = await backend.purge(limit=10)

    # Assert
    assert (claimed, replies, purged) == ([], [], 0)
    stub.client.register_script.return_value.assert_not_called()


def test_job_redis_record_with_a_missing_field_is_refused() -> None:
    """A stored job without its name cannot be read."""
    # Arrange
    fields = [None] * 11

    # Act & Assert
    with pytest.raises(ValueError, match="missing field"):
        _record("j", fields)


@pytest.fixture(scope="module")
def redis_port() -> Generator[int]:
    """Start one Redis for the module, and return its port."""
    with RedisContainer() as container:
        yield int(container.get_exposed_port(6379))


@pytest.fixture
async def backend(redis_port: int) -> AsyncGenerator[RedisJobAdapter]:
    """Provide a Redis job backend with keys of its own."""
    provider = RedisProvider(host="localhost", port=redis_port, db=0)
    backend = RedisJobAdapter(
        provider=provider, prefix=f"{{{secrets.token_hex(8)}}}:"
    )
    async with provider, backend:
        yield backend


async def _submit(
    backend: RedisJobAdapter,
    job_id: str,
    *,
    keep: timedelta | None = timedelta(hours=1),
    start_as: str | None = None,
) -> None:
    """Store a job that waits at most a minute."""
    await backend.submit(
        name=NAME,
        job_id=job_id,
        owner=None,
        payload="{}",
        context={},
        max_queued=None,
        max_wait=timedelta(minutes=1),
        keep=keep,
        start_as=start_as,
        lease=timedelta(seconds=30),
    )


async def _settle(backend: RedisJobAdapter, job_id: str) -> None:
    """End a job as runner `r`."""
    await backend.settle(
        name=NAME,
        job_id=job_id,
        runner="r",
        status=JobStatus.SUCCEEDED,
        error=None,
        result=None,
        progress=None,
    )


@pytest.mark.integration
async def test_job_redis_ended_jobs_leave_every_index(
    backend: RedisJobAdapter,
) -> None:
    """Once every job ended, no queue, wait or deadline index holds an id."""
    # Arrange
    client = backend.provider.client
    await _submit(backend, "running", start_as="r")
    await _submit(backend, "claimed")
    await _submit(backend, "cancelled")
    await backend.claim(
        slots={NAME: 1}, runner="r", lease=timedelta(seconds=30)
    )

    # Act
    await _settle(backend, "running")
    await _settle(backend, "claimed")
    await backend.cancel(name=NAME, job_id="cancelled", owner=None)

    # Assert
    deadlines, job, queue, waiting = backend._keys(NAME, "running")
    assert await client.exists(deadlines, queue, waiting) == 0
    assert await client.exists(job) == 1


@pytest.mark.parametrize(
    ("keep", "ttl"),
    [
        pytest.param(timedelta(hours=1), (3_590_000, 3_600_000), id="keep"),
        pytest.param(None, (-1, -1), id="no-limit"),
    ],
)
@pytest.mark.integration
async def test_job_redis_keep_is_the_key_expiry(
    backend: RedisJobAdapter,
    keep: timedelta | None,
    ttl: tuple[int, int],
) -> None:
    """An ended job expires after `keep`, and stays with no limit."""
    # Arrange
    client = backend.provider.client
    await _submit(backend, "j", keep=keep, start_as="r")

    # Act
    await _settle(backend, "j")

    # Assert
    low, high = ttl
    assert low <= await client.pttl(backend._keys(NAME, "j")[1]) <= high


@pytest.mark.integration
async def test_job_redis_scripts_load_again_after_a_flush(
    backend: RedisJobAdapter,
) -> None:
    """A script cache lost on restart or failover is filled again."""
    # Arrange
    await _submit(backend, "j", start_as="r")
    await backend.provider.client.script_flush()

    # Act
    [reply] = await backend.beat(
        runner="r", beats=[Beat(NAME, "j")], lease=timedelta(seconds=30)
    )

    # Assert
    assert reply.held is True
