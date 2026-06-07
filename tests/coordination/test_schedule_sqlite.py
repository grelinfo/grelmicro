"""Tests for the SQLite Schedule Adapter."""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from grelmicro.coordination.abc import ScheduleBackend
from grelmicro.coordination.errors import CoordinationSettingsValidationError
from grelmicro.coordination.sqlite import SQLiteScheduleAdapter
from grelmicro.errors import OutOfContextError
from grelmicro.providers.sqlite import SQLiteProvider

pytestmark = [pytest.mark.timeout(5)]

OLD = 100.0
NEW = 160.0
OTHER = 200.0


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncGenerator[SQLiteScheduleAdapter]:
    """Open a SQLite schedule adapter on a temp file."""
    async with SQLiteScheduleAdapter(tmp_path / "schedule.db") as adapter:
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
        SQLiteScheduleAdapter(path=":memory:", table_name=table_name)


async def test_out_of_context_errors() -> None:
    """Adapter methods raise when called outside the context manager."""
    adapter = SQLiteScheduleAdapter(path=":memory:")

    with pytest.raises(OutOfContextError):
        await adapter.claim("job", OLD)
    with pytest.raises(OutOfContextError):
        await adapter.last_fired("job")


def test_sqlite_env_var_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a path, the adapter reads `SQLITE_PATH`."""
    monkeypatch.setenv("SQLITE_PATH", "schedules.db")

    adapter = SQLiteScheduleAdapter()

    assert adapter._path == "schedules.db"


def test_sqlite_env_var_settings_validation_error() -> None:
    """A missing path raises a settings validation error."""
    with pytest.raises(
        CoordinationSettingsValidationError,
        match=(r"Could not validate settings:\n"),
    ):
        SQLiteScheduleAdapter()


def test_custom_table_name() -> None:
    """Custom `table_name=` is stored on the adapter."""
    adapter = SQLiteScheduleAdapter(path=":memory:", table_name="my_schedules")

    assert adapter._table_name == "my_schedules"


def test_provider_factory_returns_sqlite_adapter() -> None:
    """`SQLiteProvider.schedule()` returns a bound adapter on the same path."""
    provider = SQLiteProvider("schedules.db")

    adapter = provider.schedule()

    assert isinstance(adapter, SQLiteScheduleAdapter)
    assert adapter._path == "schedules.db"


def test_satisfies_protocol() -> None:
    """The adapter satisfies the `ScheduleBackend` protocol."""
    assert isinstance(SQLiteScheduleAdapter(path=":memory:"), ScheduleBackend)


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
    async with SQLiteScheduleAdapter(path) as backend:
        await backend.claim("job", OLD)
    async with SQLiteScheduleAdapter(path) as backend:
        assert await backend.last_fired("job") == OLD
