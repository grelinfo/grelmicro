"""Tests for the Postgres Schedule Adapter."""

from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Self
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from grelmicro.coordination.abc import ScheduleBackend
from grelmicro.coordination.postgres import PostgresScheduleAdapter
from grelmicro.errors import OutOfContextError
from grelmicro.providers.postgres import (
    PostgresProvider,
    PostgresProviderConfigError,
)

URL = "postgresql://test_user:test_password@test_host:1234/test_db"

OLD = 100.0
NEW = 160.0
OTHER = 200.0


# Construction and wiring (no server).


@pytest.mark.timeout(1)
@pytest.mark.parametrize(
    "table_name",
    [
        "schedules table",
        "%schedules",
        "schedules;table",
        "schedules' OR '1'='1",
        "schedules; DROP TABLE users; --",
    ],
)
def test_table_name_invalid(table_name: str) -> None:
    """Invalid SQL identifiers for the table name raise."""
    with pytest.raises(
        ValueError, match=r"Table name '.*' is not a valid SQL identifier"
    ):
        PostgresScheduleAdapter(
            provider=PostgresProvider(URL), table_name=table_name
        )


@pytest.mark.timeout(1)
async def test_out_of_context_errors() -> None:
    """Adapter methods raise when called outside the context manager."""
    adapter = PostgresScheduleAdapter(provider=PostgresProvider(URL))

    with pytest.raises(OutOfContextError):
        await adapter.claim("job", OLD)
    with pytest.raises(OutOfContextError):
        await adapter.last_fired("job")


@pytest.mark.timeout(1)
def test_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("POSTGRES_URL", URL)

    adapter = PostgresScheduleAdapter()

    assert adapter.provider.url == URL
    assert adapter._owns_provider is True


@pytest.mark.timeout(1)
def test_adapter_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = PostgresProvider(URL)
    adapter = PostgresScheduleAdapter(provider=provider)

    assert adapter.provider is provider
    assert adapter._owns_provider is False


@pytest.mark.timeout(1)
def test_adapter_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("WRITE_POSTGRES_URL", URL)

    adapter = PostgresScheduleAdapter(env_prefix="WRITE_POSTGRES_")

    assert adapter.provider.url == URL
    assert adapter.provider.env_prefix == "WRITE_POSTGRES_"


@pytest.mark.timeout(1)
def test_env_validation_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit provider surfaces `PostgresProviderConfigError`."""
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    with pytest.raises(PostgresProviderConfigError):
        PostgresScheduleAdapter()


@pytest.mark.timeout(1)
def test_custom_table_name() -> None:
    """Custom `table_name=` is stored on the adapter."""
    adapter = PostgresScheduleAdapter(
        provider=PostgresProvider(URL), table_name="my_schedules"
    )

    assert adapter._table_name == "my_schedules"


@pytest.mark.timeout(1)
def test_provider_factory_returns_postgres_adapter() -> None:
    """`PostgresProvider.schedule()` returns a bound adapter."""
    provider = PostgresProvider(URL)

    adapter = provider.schedule()

    assert isinstance(adapter, PostgresScheduleAdapter)
    assert adapter.provider is provider
    assert adapter._owns_provider is False


@pytest.mark.timeout(1)
def test_satisfies_protocol() -> None:
    """The adapter satisfies the `ScheduleBackend` protocol."""
    adapter = PostgresScheduleAdapter(provider=PostgresProvider(URL))

    assert isinstance(adapter, ScheduleBackend)


@pytest.mark.timeout(1)
def test_rebind_provider_borrows_new_provider() -> None:
    """`_rebind_provider` swaps the provider and marks it borrowed."""
    adapter = PostgresScheduleAdapter(provider=PostgresProvider(URL))
    adapter._owns_provider = True
    shared = PostgresProvider(URL)

    adapter._rebind_provider(shared)

    assert adapter.provider is shared
    assert adapter._owns_provider is False


class _StubProvider:
    """Minimal `PostgresProvider`-shaped stub tracking enter/exit calls."""

    def __init__(self) -> None:
        self.client = MagicMock()
        self.client.execute = AsyncMock()
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


@pytest.mark.timeout(1)
async def test_aenter_aexit_owned_provider_opens_and_closes_it() -> None:
    """When owned, the adapter opens, migrates, and closes its provider."""
    stub = _StubProvider()
    adapter = PostgresScheduleAdapter(provider=stub)  # ty: ignore[invalid-argument-type]
    adapter._owns_provider = True

    async with adapter:
        pass

    assert stub.enter_count == 1
    assert stub.exit_count == 1
    stub.client.execute.assert_awaited()


@pytest.mark.timeout(1)
async def test_aenter_aexit_borrowed_provider_left_alone() -> None:
    """An external provider is migrated but not entered or exited."""
    stub = _StubProvider()
    adapter = PostgresScheduleAdapter(provider=stub)  # ty: ignore[invalid-argument-type]

    async with adapter:
        pass

    assert stub.enter_count == 0
    assert stub.exit_count == 0


# Integration tests.

pytestmark: list[pytest.MarkDecorator] = []


@pytest.fixture(scope="module")
async def backend() -> AsyncGenerator[PostgresScheduleAdapter]:
    """Provide a Postgres-backed schedule adapter in a container."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        provider = PostgresProvider(
            f"postgresql://test:test@localhost:{port}/test"
        )
        async with (
            provider,
            PostgresScheduleAdapter(provider=provider) as backend,
        ):
            yield backend


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_last_fired_is_none_before_any_claim(
    backend: PostgresScheduleAdapter,
) -> None:
    """`last_fired` is `None` for a never-claimed name."""
    name = "never" + uuid4().hex
    assert await backend.last_fired(name) is None


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_claim_sets_last_fired(
    backend: PostgresScheduleAdapter,
) -> None:
    """A first claim stores the due epoch and returns `True`."""
    name = "first" + uuid4().hex
    won = await backend.claim(name, OLD)
    assert won is True
    assert await backend.last_fired(name) == OLD


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_claim_advances_to_a_newer_due(
    backend: PostgresScheduleAdapter,
) -> None:
    """A claim with a strictly greater due wins and advances the state."""
    name = "advance" + uuid4().hex
    await backend.claim(name, OLD)
    won = await backend.claim(name, NEW)
    assert won is True
    assert await backend.last_fired(name) == NEW


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_claim_rejects_an_equal_due(
    backend: PostgresScheduleAdapter,
) -> None:
    """Claiming the same due twice wins only once."""
    name = "equal" + uuid4().hex
    assert await backend.claim(name, OLD) is True
    assert await backend.claim(name, OLD) is False
    assert await backend.last_fired(name) == OLD


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_claim_rejects_an_older_due(
    backend: PostgresScheduleAdapter,
) -> None:
    """A claim with an older due loses and leaves the state untouched."""
    name = "older" + uuid4().hex
    await backend.claim(name, NEW)
    won = await backend.claim(name, OLD)
    assert won is False
    assert await backend.last_fired(name) == NEW


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_concurrent_claims_only_one_wins(
    backend: PostgresScheduleAdapter,
) -> None:
    """Many concurrent claims of one due elect a single winner."""
    from asyncio import gather  # noqa: PLC0415

    name = "concurrent" + uuid4().hex
    results = await gather(*(backend.claim(name, OLD) for _ in range(20)))
    assert results.count(True) == 1


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_names_are_independent(
    backend: PostgresScheduleAdapter,
) -> None:
    """Each schedule name keeps its own last-fire state."""
    name_a = "a" + uuid4().hex
    name_b = "b" + uuid4().hex
    await backend.claim(name_a, OLD)
    await backend.claim(name_b, OTHER)
    assert await backend.last_fired(name_a) == OLD
    assert await backend.last_fired(name_b) == OTHER
