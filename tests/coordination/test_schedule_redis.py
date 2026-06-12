"""Tests for the Redis Schedule Adapter."""

from collections.abc import AsyncGenerator, Generator
from types import TracebackType
from typing import Self
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from testcontainers.redis import RedisContainer

from grelmicro.coordination.redis import RedisScheduleAdapter
from grelmicro.providers.redis import RedisProvider

pytestmark = [pytest.mark.timeout(30)]

URL = "redis://:test_password@test_host:1234/0"


def test_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisScheduleAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_adapter_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = RedisProvider(URL)
    backend = RedisScheduleAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_adapter_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("CACHE_REDIS_URL", URL)

    backend = RedisScheduleAdapter(env_prefix="CACHE_REDIS_")

    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "CACHE_REDIS_"


def test_provider_factory_returns_redis_adapter() -> None:
    """`RedisProvider.schedule()` returns a bound adapter."""
    provider = RedisProvider(URL)

    backend = provider.schedule()

    assert isinstance(backend, RedisScheduleAdapter)
    assert backend.provider is provider


def test_rebind_provider_borrows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    monkeypatch.setenv("REDIS_URL", URL)
    backend = RedisScheduleAdapter()
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
    """When owned, the adapter opens and closes its provider."""
    stub = _StubProvider()
    backend = RedisScheduleAdapter(provider=stub)  # ty: ignore[invalid-argument-type]
    backend._owns_provider = True

    async with backend:
        pass

    assert stub.enter_count == 1
    assert stub.exit_count == 1


async def test_aenter_aexit_borrowed_provider_left_alone() -> None:
    """An external provider is not entered or exited by the adapter."""
    stub = _StubProvider()
    backend = RedisScheduleAdapter(provider=stub)  # ty: ignore[invalid-argument-type]

    async with backend:
        pass

    assert stub.enter_count == 0
    assert stub.exit_count == 0


@pytest.fixture(scope="module")
def container() -> Generator[RedisContainer, None, None]:
    """Redis Test Container."""
    with RedisContainer() as container:
        yield container


@pytest.fixture
async def backend(
    container: RedisContainer,
) -> AsyncGenerator[RedisScheduleAdapter]:
    """Redis Schedule Adapter bound to the container."""
    port = container.get_exposed_port(6379)
    provider = RedisProvider(f"redis://localhost:{port}/0")
    async with RedisScheduleAdapter(provider=provider) as backend:
        yield backend


OLD = 100.0
NEW = 160.0


@pytest.mark.integration
async def test_last_fired_is_none_before_any_claim(
    backend: RedisScheduleAdapter,
) -> None:
    """`last_fired` is `None` for a never-claimed name."""
    assert await backend.last_fired("never" + uuid4().hex) is None


@pytest.mark.integration
async def test_claim_sets_last_fired(backend: RedisScheduleAdapter) -> None:
    """A first claim stores the due epoch and returns `True`."""
    name = "claim" + uuid4().hex

    won = await backend.claim(name, OLD)

    assert won is True
    assert await backend.last_fired(name) == OLD


@pytest.mark.integration
async def test_claim_advances_to_a_newer_due(
    backend: RedisScheduleAdapter,
) -> None:
    """A claim with a strictly greater due wins and advances the state."""
    name = "advance" + uuid4().hex
    await backend.claim(name, OLD)

    won = await backend.claim(name, NEW)

    assert won is True
    assert await backend.last_fired(name) == NEW


@pytest.mark.integration
async def test_claim_rejects_an_equal_or_older_due(
    backend: RedisScheduleAdapter,
) -> None:
    """Claiming an equal or older due loses and leaves the state untouched."""
    name = "reject" + uuid4().hex
    await backend.claim(name, NEW)

    assert await backend.claim(name, NEW) is False
    assert await backend.claim(name, OLD) is False
    assert await backend.last_fired(name) == NEW
