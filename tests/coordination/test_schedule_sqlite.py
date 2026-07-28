"""Tests for the SQLite Schedule Adapter."""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from grelmicro.coordination._protocol import ScheduleBackend
from grelmicro.coordination.sqlite import SQLiteScheduleAdapter
from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.providers.sqlite import SQLiteProvider

pytestmark = [pytest.mark.timeout(5)]

OLD = 100.0
NEW = 160.0
OTHER = 200.0


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncGenerator[SQLiteScheduleAdapter]:
    """Open a SQLite schedule adapter on a temp file."""
    provider = SQLiteProvider(tmp_path / "schedule.db")
    async with provider, SQLiteScheduleAdapter(provider=provider) as adapter:
        yield adapter


# Construction and wiring (no server).


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
        SQLiteScheduleAdapter(
            provider=SQLiteProvider(":memory:"), table_name=table_name
        )


async def test_out_of_context_errors() -> None:
    """Adapter methods raise when called outside the context manager."""
    adapter = SQLiteScheduleAdapter(provider=SQLiteProvider(":memory:"))

    with pytest.raises(OutOfContextError):
        await adapter.claim("job", OLD)
    with pytest.raises(OutOfContextError):
        await adapter.last_fired("job")


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds one from `SQLITE_PATH`."""
    # Arrange
    monkeypatch.setenv("SQLITE_PATH", "schedules.db")

    # Act
    adapter = SQLiteScheduleAdapter()

    # Assert
    assert adapter.provider.path == "schedules.db"
    assert adapter._owns_provider is True


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    # Arrange
    provider = SQLiteProvider("schedules.db")

    # Act
    adapter = SQLiteScheduleAdapter(provider=provider)

    # Assert
    assert adapter.provider is provider
    assert adapter._owns_provider is False


def test_rebind_provider_borrows() -> None:
    """`_rebind_provider` swaps the provider and marks it borrowed."""
    # Arrange
    adapter = SQLiteScheduleAdapter(provider=SQLiteProvider("a.db"))
    shared = SQLiteProvider("shared.db")

    # Act
    adapter._rebind_provider(shared)

    # Assert
    assert adapter.provider is shared
    assert adapter._owns_provider is False


def test_sqlite_env_var_settings_validation_error() -> None:
    """A missing path raises a settings validation error."""
    with pytest.raises(SettingsValidationError, match="SQLITE_PATH"):
        SQLiteScheduleAdapter()


def test_custom_table_name() -> None:
    """Custom `table_name=` is stored on the adapter."""
    adapter = SQLiteScheduleAdapter(
        provider=SQLiteProvider(":memory:"), table_name="my_schedules"
    )

    assert adapter._table_name == "my_schedules"


def test_provider_factory_returns_bound_adapter() -> None:
    """`SQLiteProvider.schedule()` returns an adapter bound to the provider."""
    # Arrange
    provider = SQLiteProvider("schedules.db")

    # Act
    adapter = provider.schedule()

    # Assert
    assert isinstance(adapter, SQLiteScheduleAdapter)
    assert adapter.provider is provider


def test_satisfies_protocol() -> None:
    """The adapter satisfies the `ScheduleBackend` protocol."""
    assert isinstance(
        SQLiteScheduleAdapter(provider=SQLiteProvider(":memory:")),
        ScheduleBackend,
    )


async def test_owned_provider_opens_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An implicit (owned) provider is opened on enter and closed on exit."""
    # Arrange
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "owned.db"))
    adapter = SQLiteScheduleAdapter()  # builds and owns the provider

    # Act
    async with adapter:
        assert await adapter.claim("job", OLD) is True

    # Assert
    with pytest.raises(OutOfContextError):
        _ = adapter.provider.client


# Behavior (real file).


async def test_last_fired_is_none_before_any_claim(
    backend: SQLiteScheduleAdapter,
) -> None:
    """`last_fired` is `None` for a never-claimed name."""
    assert await backend.last_fired("job") is None


async def test_claim_sets_last_fired(
    backend: SQLiteScheduleAdapter,
) -> None:
    """A first claim stores the due epoch and returns `True`."""
    won = await backend.claim("job", OLD)
    assert won is True
    assert await backend.last_fired("job") == OLD


async def test_claim_advances_to_a_newer_due(
    backend: SQLiteScheduleAdapter,
) -> None:
    """A claim with a strictly greater due wins and advances the state."""
    await backend.claim("job", OLD)
    won = await backend.claim("job", NEW)
    assert won is True
    assert await backend.last_fired("job") == NEW


async def test_claim_rejects_an_equal_due(
    backend: SQLiteScheduleAdapter,
) -> None:
    """Claiming the same due twice wins only once."""
    assert await backend.claim("job", OLD) is True
    assert await backend.claim("job", OLD) is False
    assert await backend.last_fired("job") == OLD


async def test_claim_rejects_an_older_due(
    backend: SQLiteScheduleAdapter,
) -> None:
    """A claim with an older due loses and leaves the state untouched."""
    await backend.claim("job", NEW)
    won = await backend.claim("job", OLD)
    assert won is False
    assert await backend.last_fired("job") == NEW


async def test_concurrent_claims_only_one_wins(
    backend: SQLiteScheduleAdapter,
) -> None:
    """Many concurrent claims of one due elect a single winner."""
    results = await asyncio.gather(
        *(backend.claim("job", OLD) for _ in range(20))
    )
    assert results.count(True) == 1


async def test_names_are_independent(
    backend: SQLiteScheduleAdapter,
) -> None:
    """Each schedule name keeps its own last-fire state."""
    await backend.claim("a", OLD)
    await backend.claim("b", OTHER)
    assert await backend.last_fired("a") == OLD
    assert await backend.last_fired("b") == OTHER


async def test_state_survives_reopen(tmp_path: Path) -> None:
    """Stored fires persist across a close and reopen of the same file."""
    path = tmp_path / "durable.db"
    provider = SQLiteProvider(path)
    async with provider, SQLiteScheduleAdapter(provider=provider) as backend:
        await backend.claim("job", OLD)
    provider = SQLiteProvider(path)
    async with provider, SQLiteScheduleAdapter(provider=provider) as backend:
        assert await backend.last_fired("job") == OLD


async def test_claim_rolls_back_on_error(tmp_path: Path) -> None:
    """A failing claim rolls back the open transaction and re-raises."""
    provider = SQLiteProvider(tmp_path / "schedule.db")
    async with provider, SQLiteScheduleAdapter(provider=provider) as backend:
        conn = provider.client
        await conn.execute("DROP TABLE schedules;")

        with pytest.raises(Exception, match="no such table"):
            await backend.claim("job", OLD)

        assert conn.in_transaction is False
