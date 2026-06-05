"""Tests for the Redis Leader Election Backend."""

from collections.abc import AsyncGenerator, Generator
from types import TracebackType
from typing import Self
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from anyio import sleep
from testcontainers.redis import RedisContainer

from grelmicro.coordination.redis import RedisLeaderElectionBackend, _as_str
from grelmicro.providers.redis import RedisProvider

pytestmark = [pytest.mark.timeout(30)]

URL = "redis://:test_password@test_host:1234/0"

DURATION = 1.0
WAIT = DURATION + 0.3


def test_backend_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the backend builds its own from env vars."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisLeaderElectionBackend()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_backend_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = RedisProvider(URL)
    backend = RedisLeaderElectionBackend(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_backend_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("CACHE_REDIS_URL", URL)

    backend = RedisLeaderElectionBackend(env_prefix="CACHE_REDIS_")

    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "CACHE_REDIS_"


def test_provider_factory_returns_redis_backend() -> None:
    """`RedisProvider.leader_election()` returns a bound backend."""
    provider = RedisProvider(URL)

    backend = provider.leader_election()

    assert isinstance(backend, RedisLeaderElectionBackend)
    assert backend.provider is provider


def test_rebind_provider_borrows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    monkeypatch.setenv("REDIS_URL", URL)
    backend = RedisLeaderElectionBackend()
    assert backend._owns_provider is True
    other = RedisProvider(URL)

    backend._rebind_provider(other)

    assert backend.provider is other
    assert backend._owns_provider is False


class _StubProvider:
    """Minimal `RedisProvider`-shaped stub tracking enter/exit calls."""

    def __init__(self) -> None:
        self.client = MagicMock()
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


async def test_aenter_aexit_owned_provider_opens_and_closes_it() -> None:
    """When owned, the backend opens and closes its provider."""
    stub = _StubProvider()
    backend = RedisLeaderElectionBackend(provider=stub)  # ty: ignore[invalid-argument-type]
    backend._owns_provider = True

    async with backend:
        pass

    assert stub.enter_count == 1
    assert stub.exit_count == 1


async def test_aenter_aexit_borrowed_provider_left_alone() -> None:
    """An external provider is not entered or exited by the backend."""
    stub = _StubProvider()
    backend = RedisLeaderElectionBackend(provider=stub)  # ty: ignore[invalid-argument-type]

    async with backend:
        pass

    assert stub.enter_count == 0
    assert stub.exit_count == 0


def test_as_str_raises_on_missing_field() -> None:
    """`_as_str` raises when a stored field is missing."""
    with pytest.raises(
        ValueError, match="unexpected missing field in stored leader record"
    ):
        _as_str(None)


def test_as_str_passes_through_str() -> None:
    """`_as_str` returns a plain `str` unchanged."""
    assert _as_str("already-text") == "already-text"


@pytest.fixture(scope="module")
def monkeypatch() -> Generator[pytest.MonkeyPatch, None, None]:
    """Monkeypatch Module Scope."""
    monkeypatch = pytest.MonkeyPatch()
    yield monkeypatch
    monkeypatch.undo()


@pytest.fixture(scope="module")
def container() -> Generator[RedisContainer, None, None]:
    """Redis Test Container."""
    with RedisContainer() as container:
        yield container


@pytest.fixture
async def backend(
    container: RedisContainer,
) -> AsyncGenerator[RedisLeaderElectionBackend]:
    """Redis Leader Election Backend bound to the container."""
    port = container.get_exposed_port(6379)
    provider = RedisProvider(f"redis://localhost:{port}/0")
    async with RedisLeaderElectionBackend(provider=provider) as backend:
        yield backend


@pytest.mark.integration
async def test_acquire_fresh(backend: RedisLeaderElectionBackend) -> None:
    """A fresh election is acquired with zero transitions."""
    name = "acquire_fresh" + uuid4().hex
    token = uuid4().hex

    record = await backend.acquire_or_renew(
        name=name, token=token, duration=DURATION
    )

    assert record.holder == token
    assert record.transitions == 0
    assert record.acquired_at == record.renewed_at
    assert record.lease_duration == DURATION


@pytest.mark.integration
async def test_renew_same_holder_keeps_transitions(
    backend: RedisLeaderElectionBackend,
) -> None:
    """Renewing the same holder moves renewed_at but not acquired_at."""
    name = "renew_same" + uuid4().hex
    token = uuid4().hex

    first = await backend.acquire_or_renew(
        name=name, token=token, duration=DURATION
    )
    second = await backend.acquire_or_renew(
        name=name, token=token, duration=DURATION
    )

    assert second.holder == token
    assert second.transitions == first.transitions == 0
    assert second.acquired_at == first.acquired_at
    assert second.renewed_at >= first.renewed_at


@pytest.mark.integration
async def test_live_lease_blocks_other_holder(
    backend: RedisLeaderElectionBackend,
) -> None:
    """A different token cannot take a live lease and sees the holder."""
    name = "live_blocks" + uuid4().hex
    holder = uuid4().hex
    challenger = uuid4().hex

    await backend.acquire_or_renew(name=name, token=holder, duration=DURATION)
    record = await backend.acquire_or_renew(
        name=name, token=challenger, duration=DURATION
    )

    assert record.holder == holder
    assert record.transitions == 0


@pytest.mark.integration
async def test_takeover_after_expiry_increments_transitions(
    backend: RedisLeaderElectionBackend,
) -> None:
    """A new holder takes an expired lease and increments transitions."""
    name = "takeover" + uuid4().hex
    holder = uuid4().hex
    successor = uuid4().hex

    await backend.acquire_or_renew(name=name, token=holder, duration=DURATION)
    await sleep(WAIT)
    record = await backend.acquire_or_renew(
        name=name, token=successor, duration=DURATION
    )

    assert record.holder == successor
    assert record.transitions == 1
    assert record.acquired_at == record.renewed_at


@pytest.mark.integration
async def test_reacquire_after_expiry_keeps_transitions(
    backend: RedisLeaderElectionBackend,
) -> None:
    """Same holder reacquiring an expired lease keeps transitions."""
    name = "reacquire" + uuid4().hex
    token = uuid4().hex

    await backend.acquire_or_renew(name=name, token=token, duration=DURATION)
    await sleep(WAIT)
    record = await backend.acquire_or_renew(
        name=name, token=token, duration=DURATION
    )

    assert record.holder == token
    assert record.transitions == 0


@pytest.mark.integration
async def test_release_held_lease(
    backend: RedisLeaderElectionBackend,
) -> None:
    """The holder can release its live lease."""
    name = "release" + uuid4().hex
    token = uuid4().hex

    await backend.acquire_or_renew(name=name, token=token, duration=DURATION)
    released = await backend.release(name=name, token=token)
    again = await backend.release(name=name, token=token)

    assert released is True
    assert again is False
    assert await backend.get(name=name) is None


@pytest.mark.integration
async def test_release_other_holder_denied(
    backend: RedisLeaderElectionBackend,
) -> None:
    """A non-holder cannot release the lease."""
    name = "release_denied" + uuid4().hex
    holder = uuid4().hex
    other = uuid4().hex

    await backend.acquire_or_renew(name=name, token=holder, duration=DURATION)
    released = await backend.release(name=name, token=other)

    assert released is False


@pytest.mark.integration
async def test_get_returns_none_after_expiry(
    backend: RedisLeaderElectionBackend,
) -> None:
    """`get` returns the live record then `None` once it expires."""
    name = "get_expiry" + uuid4().hex
    token = uuid4().hex

    await backend.acquire_or_renew(name=name, token=token, duration=DURATION)
    live = await backend.get(name=name)
    await sleep(WAIT)
    expired = await backend.get(name=name)

    assert live is not None
    assert live.holder == token
    assert expired is None


@pytest.mark.integration
async def test_metadata_round_trips(
    backend: RedisLeaderElectionBackend,
) -> None:
    """Metadata stored on acquire is returned on get."""
    name = "metadata" + uuid4().hex
    token = uuid4().hex
    metadata = {"pod": "worker-1", "region": "eu-west"}

    await backend.acquire_or_renew(
        name=name, token=token, duration=DURATION, metadata=metadata
    )
    record = await backend.get(name=name)

    assert record is not None
    assert record.metadata == metadata
