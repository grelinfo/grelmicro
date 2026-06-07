"""Tests for the Postgres leader election backend."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_mock

from grelmicro.coordination.abc import LeaderElectionBackend
from grelmicro.coordination.postgres import (
    PostgresLeaderElectionBackend,
    _decode_metadata,
)
from grelmicro.errors import OutOfContextError
from grelmicro.providers.postgres import (
    PostgresProvider,
    PostgresProviderConfigError,
)

URL = "postgresql://test_user:test_password@test_host:1234/test_db"


@pytest.mark.timeout(1)
@pytest.mark.parametrize(
    "table_name",
    [
        "leader table",
        "%leader",
        "leader;table",
        "leader' OR '1'='1",
        "leader; DROP TABLE users; --",
    ],
)
def test_table_name_invalid(table_name: str) -> None:
    """Invalid SQL identifiers for the table name raise."""
    with pytest.raises(
        ValueError, match=r"Table name '.*' is not a valid SQL identifier"
    ):
        PostgresLeaderElectionBackend(
            provider=PostgresProvider(URL), table_name=table_name
        )


@pytest.mark.timeout(1)
async def test_out_of_context_errors() -> None:
    """Backend methods raise when called outside the context manager."""
    backend = PostgresLeaderElectionBackend(provider=PostgresProvider(URL))
    name = "election"
    token = "token"

    with pytest.raises(OutOfContextError):
        await backend.acquire_or_renew(name=name, token=token, duration=1)
    with pytest.raises(OutOfContextError):
        await backend.release(name=name, token=token)
    with pytest.raises(OutOfContextError):
        await backend.get(name=name)


@pytest.mark.timeout(1)
def test_backend_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the backend builds its own from env vars."""
    monkeypatch.setenv("POSTGRES_URL", URL)

    backend = PostgresLeaderElectionBackend()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


@pytest.mark.timeout(1)
def test_backend_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = PostgresProvider(URL)
    backend = PostgresLeaderElectionBackend(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


@pytest.mark.timeout(1)
def test_backend_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("WRITE_POSTGRES_URL", URL)

    backend = PostgresLeaderElectionBackend(env_prefix="WRITE_POSTGRES_")

    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "WRITE_POSTGRES_"


@pytest.mark.timeout(1)
def test_env_validation_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit provider surfaces `PostgresProviderConfigError`."""
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    with pytest.raises(PostgresProviderConfigError):
        PostgresLeaderElectionBackend()


@pytest.mark.timeout(1)
def test_custom_table_name() -> None:
    """Custom `table_name=` is stored on the backend."""
    backend = PostgresLeaderElectionBackend(
        provider=PostgresProvider(URL), table_name="my_leaders"
    )

    assert backend._table_name == "my_leaders"


@pytest.mark.timeout(1)
def test_provider_factory() -> None:
    """`provider.leader_election()` binds a backend to the provider."""
    provider = PostgresProvider(URL)

    backend = provider.leader_election()

    assert isinstance(backend, PostgresLeaderElectionBackend)
    assert backend.provider is provider
    assert backend._owns_provider is False


@pytest.mark.timeout(1)
def test_satisfies_protocol() -> None:
    """The backend satisfies the `LeaderElectionBackend` protocol."""
    backend = PostgresLeaderElectionBackend(provider=PostgresProvider(URL))

    assert isinstance(backend, LeaderElectionBackend)


@pytest.mark.timeout(1)
def test_rebind_provider_borrows_new_provider() -> None:
    """`_rebind_provider` swaps the provider and marks it borrowed."""
    owned = PostgresProvider(URL)
    backend = PostgresLeaderElectionBackend(provider=owned)
    backend._owns_provider = True
    shared = PostgresProvider(URL)

    backend._rebind_provider(shared)

    assert backend.provider is shared
    assert backend._owns_provider is False


@pytest.mark.timeout(1)
async def test_owned_provider_lifecycle(
    mocker: pytest_mock.MockerFixture,
) -> None:
    """An owned provider is entered and exited with the backend."""
    provider = PostgresProvider(URL)
    aenter = mocker.patch.object(
        provider, "__aenter__", AsyncMock(return_value=provider)
    )
    aexit = mocker.patch.object(provider, "__aexit__", AsyncMock())
    backend = PostgresLeaderElectionBackend(
        provider=provider, auto_migrate=False
    )
    backend._owns_provider = True

    async with backend:
        aenter.assert_awaited_once()
        aexit.assert_not_awaited()

    aexit.assert_awaited_once()


@pytest.mark.timeout(1)
async def test_borrowed_provider_lifecycle_left_alone(
    mocker: pytest_mock.MockerFixture,
) -> None:
    """A borrowed provider is never entered or exited by the backend."""
    provider = PostgresProvider(URL)
    aenter = mocker.patch.object(
        provider, "__aenter__", AsyncMock(return_value=provider)
    )
    aexit = mocker.patch.object(provider, "__aexit__", AsyncMock())
    backend = PostgresLeaderElectionBackend(
        provider=provider, auto_migrate=False
    )

    async with backend:
        pass

    aenter.assert_not_awaited()
    aexit.assert_not_awaited()


@pytest.mark.timeout(1)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, {}),
        ('{"pod": "worker-1"}', {"pod": "worker-1"}),
        ({"pod": "worker-1"}, {"pod": "worker-1"}),
    ],
)
def test_decode_metadata(value: object, expected: dict[str, str]) -> None:
    """Decode jsonb values from None, JSON string, and dict."""
    assert _decode_metadata(value) == expected


# Integration tests.

pytestmark: list[pytest.MarkDecorator] = []

_DURATION = 1.0
_EXPIRE_WAIT = _DURATION + 0.3


@pytest.fixture(scope="module")
async def backend() -> AsyncGenerator[PostgresLeaderElectionBackend]:
    """Provide a Postgres-backed leader election backend in a container."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        provider = PostgresProvider(
            f"postgresql://test:test@localhost:{port}/test"
        )
        async with (
            provider,
            PostgresLeaderElectionBackend(provider=provider) as backend,
        ):
            yield backend


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_acquire(
    backend: PostgresLeaderElectionBackend,
) -> None:
    """A fresh election is acquired with transitions at zero."""
    name = "test_acquire" + uuid4().hex
    token = uuid4().hex

    record = await backend.acquire_or_renew(
        name=name, token=token, duration=_DURATION
    )

    assert record.holder == token
    assert record.transitions == 0
    assert record.acquired_at == record.renewed_at


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_renew_keeps_transitions(
    backend: PostgresLeaderElectionBackend,
) -> None:
    """Renewing the same holder moves renewed_at but not transitions."""
    name = "test_renew" + uuid4().hex
    token = uuid4().hex

    first = await backend.acquire_or_renew(
        name=name, token=token, duration=_DURATION
    )
    second = await backend.acquire_or_renew(
        name=name, token=token, duration=_DURATION
    )

    assert second.holder == token
    assert second.transitions == first.transitions == 0
    assert second.acquired_at == first.acquired_at
    assert second.renewed_at >= first.renewed_at


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_live_lease_not_taken(
    backend: PostgresLeaderElectionBackend,
) -> None:
    """A live lease cannot be taken by another holder."""
    name = "test_live" + uuid4().hex
    token1 = uuid4().hex
    token2 = uuid4().hex

    first = await backend.acquire_or_renew(
        name=name, token=token1, duration=_DURATION
    )
    second = await backend.acquire_or_renew(
        name=name, token=token2, duration=_DURATION
    )

    assert first.holder == token1
    assert second.holder == token1
    assert second.transitions == 0


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_takeover_after_expiry(
    backend: PostgresLeaderElectionBackend,
) -> None:
    """A new holder takes over after expiry and bumps transitions."""
    from asyncio import sleep  # noqa: PLC0415

    name = "test_takeover" + uuid4().hex
    token1 = uuid4().hex
    token2 = uuid4().hex

    await backend.acquire_or_renew(name=name, token=token1, duration=_DURATION)
    await sleep(_EXPIRE_WAIT)
    record = await backend.acquire_or_renew(
        name=name, token=token2, duration=_DURATION
    )

    assert record.holder == token2
    assert record.transitions == 1


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_release(
    backend: PostgresLeaderElectionBackend,
) -> None:
    """Release returns True for the holder, False for non-holders."""
    name = "test_release" + uuid4().hex
    token = uuid4().hex
    other = uuid4().hex

    await backend.acquire_or_renew(name=name, token=token, duration=_DURATION)

    assert await backend.release(name=name, token=other) is False
    assert await backend.release(name=name, token=token) is True
    assert await backend.release(name=name, token=token) is False


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_get_live_and_expired(
    backend: PostgresLeaderElectionBackend,
) -> None:
    """Get returns the live record then None after expiry."""
    from asyncio import sleep  # noqa: PLC0415

    name = "test_get" + uuid4().hex
    token = uuid4().hex

    assert await backend.get(name=name) is None
    await backend.acquire_or_renew(name=name, token=token, duration=_DURATION)
    live = await backend.get(name=name)
    assert live is not None
    assert live.holder == token

    await sleep(_EXPIRE_WAIT)
    assert await backend.get(name=name) is None


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_metadata_roundtrip(
    backend: PostgresLeaderElectionBackend,
) -> None:
    """Metadata stored as jsonb round-trips through acquire and get."""
    name = "test_metadata" + uuid4().hex
    token = uuid4().hex
    metadata = {"pod": "worker-1", "region": "eu-west-1"}

    record = await backend.acquire_or_renew(
        name=name, token=token, duration=_DURATION, metadata=metadata
    )

    assert dict(record.metadata) == metadata
    fetched = await backend.get(name=name)
    assert fetched is not None
    assert dict(fetched.metadata) == metadata
