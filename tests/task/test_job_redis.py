"""Test what only the Redis job backend promises."""

import asyncio
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


def test_job_redis_valkey_provider_factory_returns_bound_adapter() -> None:
    """`ValkeyProvider.job_backend()` returns the Redis adapter on Valkey."""
    # Arrange
    provider = ValkeyProvider(URL)

    # Act
    backend = provider.job_backend(prefix="jobs:")

    # Assert
    assert isinstance(backend, RedisJobAdapter)
    assert backend.provider is provider
    assert backend._key_prefix == "jobs:"


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


def test_job_redis_cluster_prefix_without_hash_tag_takes_its_own() -> None:
    """On a Redis Cluster, a prefix with no hash tag goes under `{...job}:`."""
    # Arrange
    provider = RedisProvider("redis+cluster://h1:6379,h2:6379")

    # Act
    backend = RedisJobAdapter(provider=provider, prefix="jobs:")

    # Assert
    assert backend._base == "{jobs:job}:"


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
        backend._owned_key("{odd}name", "{odd}owner"),
    ]

    # Assert
    assert {key_slot(key.encode()) for key in keys} == {key_slot(b"app")}


def test_job_redis_owner_index_never_names_the_owner() -> None:
    """The owner index key holds a hash of the owner, never the owner."""
    # Arrange
    backend = RedisJobAdapter(provider=RedisProvider(URL), prefix="p:")

    # Act
    alice = backend._owned_key(NAME, '["https://idp", "alice"]')
    bob = backend._owned_key(NAME, "bob")

    # Assert
    assert "alice" not in alice
    assert "idp" not in alice
    assert alice.startswith(f"p:job:own:{NAME}:")
    assert alice != bob


class _StubProvider:
    """Minimal `RedisProvider`-shaped stub tracking enter and exit calls."""

    def __init__(self) -> None:
        self.client = MagicMock()
        self.is_cluster = False
        self.prefix = ""
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
    """A claim with no free slot and an empty beat reach no server."""
    # Arrange
    stub = _StubProvider()
    backend = RedisJobAdapter(provider=stub)  # ty: ignore[invalid-argument-type]
    lease = timedelta(seconds=30)

    # Act
    claimed = await backend.claim(slots={NAME: 0}, runner="r", lease=lease)
    replies = await backend.beat(runner="r", beats=[], lease=lease)

    # Assert
    assert (claimed, replies) == ([], [])
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
    owner: str | None = None,
    max_kept_succeeded: int | None = None,
) -> None:
    """Store a job that waits at most a minute."""
    await backend.submit(
        name=NAME,
        job_id=job_id,
        owner=owner,
        payload="{}",
        context={},
        max_queued=None,
        max_wait=timedelta(minutes=1),
        max_run=None,
        keep=keep,
        max_kept_succeeded=max_kept_succeeded,
        max_kept_failed=None,
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


OWNER = "alice"


async def _owned(backend: RedisJobAdapter) -> int:
    """Return how many jobs the owner index holds."""
    client = backend.provider.client
    return await client.zcard(backend._owned_key(NAME, OWNER))


async def _due(backend: RedisJobAdapter) -> int:
    """Return how many owner index entries wait for their expiry."""
    return await backend.provider.client.zcard(f"{backend._base}expires")


async def _cancel_many(
    backend: RedisJobAdapter, count: int, keep: timedelta | None
) -> None:
    """Store `count` owned jobs, then cancel each while it is queued."""
    for index in range(count):
        await _submit(backend, f"j{index}", keep=keep, owner=OWNER)
        await backend.cancel(name=NAME, job_id=f"j{index}", owner=OWNER)


@pytest.mark.integration
async def test_job_redis_purge_drops_expired_jobs_from_the_owner_index(
    backend: RedisJobAdapter,
) -> None:
    """An owner who never lists keeps no expired job in the index.

    Each ended job leaves the index once its `keep` passed, at a `purge`,
    even while another job of the owner is still open.
    """
    # Arrange
    await _submit(backend, "open", owner=OWNER)
    await _cancel_many(backend, 30, timedelta(milliseconds=5))
    await asyncio.sleep(0.05)

    # Act
    purged = await backend.purge(limit=100)

    # Assert
    assert purged == 30  # noqa: PLR2004
    assert await _owned(backend) == 1
    assert await _due(backend) == 0


@pytest.mark.integration
async def test_job_redis_purge_drops_up_to_its_limit(
    backend: RedisJobAdapter,
) -> None:
    """A `purge` drops at most `limit` entries, and the next one the rest."""
    # Arrange
    await _cancel_many(backend, 15, timedelta(milliseconds=5))
    await asyncio.sleep(0.05)

    # Act
    first = await backend.purge(limit=10)
    second = await backend.purge(limit=10)

    # Assert
    assert (first, second) == (10, 5)
    assert await _owned(backend) == 0


@pytest.mark.integration
async def test_job_redis_purge_keeps_a_job_still_kept(
    backend: RedisJobAdapter,
) -> None:
    """A job inside its `keep`, or kept with no limit, stays in the index."""
    # Arrange
    await _cancel_many(backend, 1, timedelta(hours=1))
    await _submit(backend, "forever", keep=None, owner=OWNER)
    await backend.cancel(name=NAME, job_id="forever", owner=OWNER)

    # Act
    purged = await backend.purge(limit=100)

    # Assert
    assert purged == 0
    assert await _owned(backend) == 2  # noqa: PLR2004
    assert await _due(backend) == 1


@pytest.mark.integration
async def test_job_redis_purge_keeps_a_new_job_with_the_same_id(
    backend: RedisJobAdapter,
) -> None:
    """A due entry of a forgotten job never drops a new job with its id."""
    # Arrange
    await _cancel_many(backend, 1, timedelta(milliseconds=5))
    await asyncio.sleep(0.05)
    await _submit(backend, "j0", owner=OWNER)

    # Act
    purged = await backend.purge(limit=100)

    # Assert
    assert purged == 0
    assert await _owned(backend) == 1
    assert await _due(backend) == 0


@pytest.mark.integration
async def test_job_redis_owner_index_drops_a_job_kept_for_zero(
    backend: RedisJobAdapter,
) -> None:
    """A job that keeps nothing leaves the index as it ends."""
    # Arrange
    await _submit(backend, "j", keep=timedelta(0), start_as="r", owner=OWNER)

    # Act
    await _settle(backend, "j")

    # Assert
    assert await _owned(backend) == 0
    assert await _due(backend) == 0


@pytest.mark.integration
async def test_job_redis_list_drops_a_member_whose_job_is_gone(
    backend: RedisJobAdapter,
) -> None:
    """A member whose job hash is gone, such as evicted, leaves on a list."""
    # Arrange
    await _submit(backend, "gone", start_as="r", owner=OWNER)
    await _settle(backend, "gone")
    await _submit(backend, "kept", owner=OWNER)
    await backend.provider.client.delete(backend._keys(NAME, "gone")[1])

    # Act
    listed = await backend.list(
        name=NAME, owner=OWNER, status=None, limit=10, after=None
    )

    # Assert
    assert [record.id for record in listed.records] == ["kept"]
    assert await _owned(backend) == 1


async def _kept_index(backend: RedisJobAdapter) -> int:
    """Return how many members the kept index of succeeded jobs holds."""
    client = backend.provider.client
    return await client.zcard(f"{backend._base}kept:succeeded:{NAME}")


async def _kept_ends(backend: RedisJobAdapter) -> list[bytes]:
    """Return the members of the expiry twin of the kept index."""
    client = backend.provider.client
    return await client.zrange(
        f"{backend._base}kept-ends:succeeded:{NAME}", 0, -1
    )


@pytest.mark.integration
async def test_job_redis_max_kept_leaves_no_entry_of_a_forgotten_job(
    backend: RedisJobAdapter,
) -> None:
    """A job the cap forgets leaves its hash and every index entry behind."""
    # Arrange
    client = backend.provider.client
    for job_id in ("old", "new"):
        await _submit(
            backend, job_id, start_as="r", owner=OWNER, max_kept_succeeded=1
        )

    # Act
    await _settle(backend, "old")
    await _settle(backend, "new")

    # Assert
    deadlines, job, queue, waiting = backend._keys(NAME, "old")
    assert await client.exists(job, deadlines, queue, waiting) == 0
    assert await client.zrange(backend._owned_key(NAME, OWNER), 0, -1) == [
        b"new"
    ]
    assert await _kept_index(backend) == 1
    assert await _kept_ends(backend) == [b"new"]
    assert await _due(backend) == 2  # noqa: PLR2004


@pytest.mark.integration
async def test_job_redis_purge_cleans_the_kept_index(
    backend: RedisJobAdapter,
) -> None:
    """A job past its `keep` leaves the kept index at a `purge`."""
    # Arrange
    await _submit(
        backend,
        "j",
        start_as="r",
        owner=OWNER,
        keep=timedelta(milliseconds=5),
        max_kept_succeeded=10,
    )
    await _settle(backend, "j")
    await asyncio.sleep(0.05)

    # Act
    purged = await backend.purge(limit=100)

    # Assert
    assert purged == 2  # noqa: PLR2004
    assert await _kept_index(backend) == 0
    assert await _kept_ends(backend) == []
    assert await _owned(backend) == 0
    assert await _due(backend) == 0


@pytest.mark.integration
async def test_job_redis_max_kept_drops_a_member_whose_job_is_gone(
    backend: RedisJobAdapter,
) -> None:
    """A member whose job hash is gone, such as evicted, frees its place."""
    # Arrange
    await _submit(backend, "gone", start_as="r", max_kept_succeeded=1)
    await _settle(backend, "gone")
    await backend.provider.client.delete(backend._keys(NAME, "gone")[1])
    await _submit(backend, "kept", start_as="r", max_kept_succeeded=1)

    # Act
    await _settle(backend, "kept")

    # Assert
    assert await _kept_index(backend) == 1
    assert await _due(backend) == 1
    assert await backend.get(name=NAME, job_id="kept", owner=None) is not None


@pytest.mark.integration
async def test_job_redis_max_kept_writes_no_index_without_a_cap(
    backend: RedisJobAdapter,
) -> None:
    """A job with no cap adds nothing to a kept index."""
    # Arrange
    await _submit(backend, "j", start_as="r")

    # Act
    await _settle(backend, "j")

    # Assert
    keys = await backend.provider.client.keys(f"{backend._base}kept:*")
    assert keys == []
