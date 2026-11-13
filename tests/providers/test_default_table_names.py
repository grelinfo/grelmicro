"""A default table name that takes the `grelmicro_` prefix warns until 0.46.0."""

import inspect
import re
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.coordination.postgres import (
    PostgresLockAdapter,
    PostgresScheduleAdapter,
)
from grelmicro.coordination.sqlite import (
    SQLiteLockAdapter,
    SQLiteReadWriteLockAdapter,
    SQLiteScheduleAdapter,
)
from grelmicro.errors import (
    Grelmicro046Warning,
    GrelmicroFutureWarning,
    SettingsValidationError,
)
from grelmicro.providers._table_names import TableNames
from grelmicro.providers.postgres import PostgresConfig, PostgresProvider
from grelmicro.providers.sqlite import SQLiteConfig, SQLiteProvider
from grelmicro.resilience.circuitbreaker.postgres import (
    PostgresCircuitBreakerAdapter,
)
from grelmicro.resilience.circuitbreaker.sqlite import (
    SQLiteCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.postgres import (
    PostgresRateLimiterAdapter,
)
from grelmicro.resilience.ratelimiter.sqlite import SQLiteRateLimiterAdapter
from tests._future import DEFAULT_TABLE_NAME_ADAPTERS
from tests._postgres import mock_pool

_URL = "postgresql://test:test@localhost:5432/test"
"""A Postgres URL the unit tests never connect to."""

_POSTGRES: list[tuple[type, str, str]] = [
    (PostgresLockAdapter, "locks", "grelmicro_locks"),
    (PostgresScheduleAdapter, "schedules", "grelmicro_schedules"),
    (
        PostgresRateLimiterAdapter,
        "grelmicro_rate_limiter",
        "grelmicro_ratelimiter",
    ),
    (
        PostgresCircuitBreakerAdapter,
        "grelmicro_circuit_breaker",
        "grelmicro_circuitbreaker",
    ),
]
"""Each Postgres adapter, with its default table name and the 0.46.0 one."""

_SQLITE: list[tuple[type, str, str]] = [
    (SQLiteLockAdapter, "locks", "grelmicro_locks"),
    (SQLiteReadWriteLockAdapter, "rwlocks", "grelmicro_rwlocks"),
    (SQLiteScheduleAdapter, "schedules", "grelmicro_schedules"),
    (
        SQLiteRateLimiterAdapter,
        "grelmicro_rate_limiter",
        "grelmicro_ratelimiter",
    ),
    (
        SQLiteCircuitBreakerAdapter,
        "grelmicro_circuit_breaker",
        "grelmicro_circuitbreaker",
    ),
]
"""Each SQLite adapter, with its default table name and the 0.46.0 one."""


def _params(
    adapters: list[tuple[type, str, str]],
) -> list[Any]:
    """Return one test case per adapter, named after it."""
    return [
        pytest.param(adapter, old, new, id=adapter.__name__)
        for adapter, old, new in adapters
    ]


_FACTORIES: dict[type, str] = {
    PostgresLockAdapter: "lock_backend",
    PostgresScheduleAdapter: "schedule_backend",
    PostgresRateLimiterAdapter: "ratelimiter_backend",
    PostgresCircuitBreakerAdapter: "circuitbreaker_backend",
    SQLiteLockAdapter: "lock_backend",
    SQLiteReadWriteLockAdapter: "readwritelock_backend",
    SQLiteScheduleAdapter: "schedule_backend",
    SQLiteRateLimiterAdapter: "ratelimiter_backend",
    SQLiteCircuitBreakerAdapter: "circuitbreaker_backend",
}
"""The provider factory that builds each adapter."""


def _provider(adapter: type) -> Any:  # noqa: ANN401
    """Return a provider for `adapter` that never connects."""
    if adapter.__name__.startswith("Postgres"):
        return PostgresProvider(_URL)
    return SQLiteProvider(":memory:")


def _default_message(adapter: type, old: str, new: str) -> str:
    """Return the warning a default table name gives, word for word."""
    provider = _provider(adapter)
    assert old
    return (
        f"`{adapter.__name__}(table_name=)` defaults to `{new}` from 0.46.0. "
        f'Pass `table_names="legacy"` to `{type(provider).__name__}` to keep '
        'the current table names, or `table_names="prefixed"` to adopt the '
        "new ones now. A `table_name=` passed to the store wins."
    )


def _split_message(adapter: type, old: str, new: str) -> str:
    """Return the warning two default tables give, word for word."""
    return (
        f"`{adapter.__name__}` opens `{old}` while `{new}` exists, the table "
        "it defaults to from 0.46.0. Replicas may be split across the two "
        "tables, each seeing only its own state. Give every replica the same "
        "`table_names=` on its provider."
    )


@contextmanager
def _future_warnings() -> Generator[list[warnings.WarningMessage]]:
    """Record every future warning the body emits, and only those."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", GrelmicroFutureWarning)
        found: list[warnings.WarningMessage] = []
        yield found
    found.extend(
        warning
        for warning in caught
        if issubclass(warning.category, GrelmicroFutureWarning)
    )


def _build_default(adapter: type, **kwargs: object) -> Any:  # noqa: ANN401
    """Build `adapter` with its table name left at its default."""
    return adapter(provider=_provider(adapter), **kwargs)


def test_every_adapter_with_a_changing_default_is_covered() -> None:
    """The tests and the shared quiet filter list the same adapters."""
    assert sorted(a.__name__ for a in _FACTORIES) == sorted(
        DEFAULT_TABLE_NAME_ADAPTERS
    )


@pytest.mark.parametrize(
    ("adapter", "old", "new"), _params(_POSTGRES + _SQLITE)
)
def test_building_with_the_default_warns_nothing(
    adapter: type, old: str, new: str
) -> None:
    """A store warns when it opens, so building one is quiet."""
    # Act
    with _future_warnings() as caught:
        built = adapter(provider=_provider(adapter))
        made = getattr(_provider(adapter), _FACTORIES[adapter])()

    # Assert
    assert caught == []
    assert built._table_name == old
    assert made._table_name == old
    assert new


@pytest.mark.parametrize(
    ("adapter", "old", "new"), _params(_POSTGRES + _SQLITE)
)
def test_the_default_value_is_unchanged(
    adapter: type, old: str, new: str
) -> None:
    """The signature still shows the current default, not the 0.46.0 one."""
    default = inspect.signature(adapter).parameters["table_name"].default

    assert default == old
    assert default != new
    assert repr(default) == repr(old)


@pytest.mark.parametrize("name", ["old", "new"])
@pytest.mark.parametrize(("adapter", "old", "new"), _params(_SQLITE))
async def test_an_explicit_table_name_warns_nothing(
    tmp_path: Path, adapter: type, old: str, new: str, name: str
) -> None:
    """Passing either name, the old default spelled out included, is quiet."""
    # Arrange
    table_name = old if name == "old" else new
    async with SQLiteProvider(tmp_path / "app.db") as provider:
        backend = adapter(provider=provider, table_name=table_name)

        # Act
        with _future_warnings() as caught:
            async with backend:
                pass

    # Assert
    assert caught == []
    assert backend._table_name == table_name


async def test_a_bare_provider_warns_once_per_store_when_the_app_opens(
    tmp_path: Path,
) -> None:
    """`uses=[provider]` builds quietly and warns for each store it opens."""
    # Arrange
    with _future_warnings() as built:
        app = Grelmicro(uses=[SQLiteProvider(tmp_path / "app.db")])

    # Act
    with _future_warnings() as caught:
        async with app:
            pass

    # Assert
    assert built == []
    assert sorted(str(warning.message) for warning in caught) == sorted(
        _default_message(adapter, old, new) for adapter, old, new in _SQLITE
    )


async def test_a_replaced_store_never_opens_so_it_never_warns(
    tmp_path: Path,
) -> None:
    """A lock backend passed beside the provider leaves its default quiet."""
    # Arrange
    provider = SQLiteProvider(tmp_path / "app.db")
    coordination = Coordination(provider, lock=MemoryLockAdapter())

    # Act
    with _future_warnings() as caught:
        async with provider, coordination:
            pass

    # Assert
    assert sorted(str(warning.message) for warning in caught) == sorted(
        _default_message(adapter, old, new)
        for adapter, old, new in _SQLITE
        if adapter in {SQLiteReadWriteLockAdapter, SQLiteScheduleAdapter}
    )


async def _create_table(provider: SQLiteProvider, name: str) -> None:
    """Create an empty table `name`, as a 0.46 replica would."""
    await provider.client.execute(f"CREATE TABLE {name} (name TEXT);")


@pytest.mark.parametrize(("adapter", "old", "new"), _params(_SQLITE))
async def test_sqlite_opening_the_default_warns_once_at_the_caller(
    tmp_path: Path, adapter: type, old: str, new: str
) -> None:
    """Opening a store on its default name warns once, word for word."""
    # Arrange
    async with SQLiteProvider(tmp_path / "app.db") as provider:
        backend = adapter(provider=provider)

        # Act
        with _future_warnings() as caught:
            async with backend:
                pass

        # Assert
        assert [str(warning.message) for warning in caught] == [
            _default_message(adapter, old, new)
        ]
        assert caught[0].category is Grelmicro046Warning
        assert caught[0].filename == __file__
        async with provider.client.execute(
            "SELECT name FROM main.sqlite_master WHERE name IN (?, ?);",
            (old, new),
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == [old]


@pytest.mark.parametrize(("adapter", "old", "new"), _params(_SQLITE))
async def test_sqlite_opening_the_old_default_beside_the_new_table_warns(
    tmp_path: Path, adapter: type, old: str, new: str
) -> None:
    """A rollback from 0.46 leaves both tables, and opening says so too."""
    # Arrange
    async with SQLiteProvider(tmp_path / "app.db") as provider:
        await _create_table(provider, new)
        backend = adapter(provider=provider)

        # Act
        with _future_warnings() as caught:
            async with backend:
                pass

    # Assert
    assert [str(warning.message) for warning in caught] == [
        _default_message(adapter, old, new),
        _split_message(adapter, old, new),
    ]
    assert {warning.filename for warning in caught} == {__file__}


@pytest.mark.parametrize(("adapter", "old", "new"), _params(_SQLITE))
async def test_sqlite_an_explicit_old_name_beside_the_new_table_is_quiet(
    tmp_path: Path, adapter: type, old: str, new: str
) -> None:
    """A name passed in is the user's choice, so opening checks nothing."""
    # Arrange
    async with SQLiteProvider(tmp_path / "app.db") as provider:
        await _create_table(provider, new)
        backend = adapter(provider=provider, table_name=old)

        # Act
        with _future_warnings() as caught:
            async with backend:
                pass

    # Assert
    assert caught == []


def _mocked(
    adapter: type, row: tuple[str | None, str | None], **kwargs: object
) -> tuple[Any, Any]:
    """Return a default `adapter` on a pool whose catalog check returns `row`."""
    provider = PostgresProvider(_URL)
    pool = mock_pool()
    pool.connection.fetchrow = AsyncMock(return_value=row)
    provider._pool = pool
    backend = _build_default(adapter, **kwargs)
    backend._rebind_provider(provider)
    return backend, pool


@pytest.mark.parametrize(("adapter", "old", "new"), _params(_POSTGRES))
async def test_postgres_setup_checks_both_names_in_its_transaction(
    adapter: type, old: str, new: str
) -> None:
    """Setup reports the schema-qualified names the catalog resolves."""
    # Arrange
    backend, pool = _mocked(adapter, (f"app.{old}", f"app.{new}"))

    # Act
    with _future_warnings() as caught:
        async with backend:
            pass

    # Assert
    query, *args = pool.connection.fetchrow.await_args.args
    assert "to_regclass($1)" in query
    assert "to_regclass($2)" in query
    assert args == [old, new]
    assert [str(warning.message) for warning in caught] == [
        _default_message(adapter, old, new),
        _split_message(adapter, f"app.{old}", f"app.{new}"),
    ]
    assert {warning.filename for warning in caught} == {__file__}


@pytest.mark.parametrize(("adapter", "old", "new"), _params(_POSTGRES))
async def test_postgres_opening_the_default_warns_once_at_the_caller(
    adapter: type, old: str, new: str
) -> None:
    """Only the old table resolves, so opening warns about the default only."""
    # Arrange
    backend, _ = _mocked(adapter, (f"public.{old}", None))

    # Act
    with _future_warnings() as caught:
        async with backend:
            pass

    # Assert
    assert [str(warning.message) for warning in caught] == [
        _default_message(adapter, old, new)
    ]
    assert caught[0].filename == __file__


_NO_MIGRATE = [
    case
    for case in _params(_POSTGRES + _SQLITE)
    if "auto_migrate" in inspect.signature(case.values[0]).parameters
]
"""Each adapter that can skip creating its table."""


@pytest.mark.parametrize(("adapter", "old", "new"), _NO_MIGRATE)
async def test_opening_without_auto_migrate_still_warns(
    tmp_path: Path, adapter: type, old: str, new: str
) -> None:
    """A store whose table the user creates still changes its name in 0.46."""
    # Arrange
    if adapter.__name__.startswith("Postgres"):
        backend, _ = _mocked(adapter, (None, None), auto_migrate=False)
    else:
        backend = adapter(
            provider=SQLiteProvider(tmp_path / "app.db"), auto_migrate=False
        )

    # Act
    with _future_warnings() as caught:
        async with backend:
            pass

    # Assert
    assert [str(warning.message) for warning in caught] == [
        _default_message(adapter, old, new)
    ]


async def test_postgres_names_the_old_table_as_passed_when_unresolved() -> None:
    """A table the catalog cannot resolve is named as the adapter calls it."""
    # Arrange
    backend, _ = _mocked(PostgresLockAdapter, (None, "public.grelmicro_locks"))

    # Act
    with _future_warnings() as caught:
        async with backend:
            pass

    # Assert
    assert re.search(
        re.escape("opens `locks` while `public.grelmicro_locks` exists"),
        str(caught[-1].message),
    )


def _provider_with(adapter: type, table_names: Any) -> Any:  # noqa: ANN401
    """Return a provider for `adapter` with `table_names`, never connecting."""
    if adapter.__name__.startswith("Postgres"):
        return PostgresProvider(_URL, table_names=table_names)
    return SQLiteProvider(":memory:", table_names=table_names)


@pytest.mark.parametrize(
    ("adapter", "old", "new"), _params(_POSTGRES + _SQLITE)
)
@pytest.mark.parametrize("table_names", ["legacy", "prefixed", "PREFIXED"])
def test_the_provider_table_names_pick_each_default(
    adapter: type, old: str, new: str, table_names: str
) -> None:
    """`legacy` keeps the current name and `prefixed` adopts the new one."""
    # Act
    provider = _provider_with(adapter, table_names=table_names)
    built = adapter(provider=provider)
    made = getattr(provider, _FACTORIES[adapter])()

    # Assert
    expected = old if table_names == "legacy" else new
    assert built._table_name == expected
    assert made._table_name == expected
    assert provider.table_names == table_names.lower()


@pytest.mark.parametrize(("adapter", "old", "new"), _params(_SQLITE))
@pytest.mark.parametrize("table_names", ["legacy", "prefixed"])
async def test_a_provider_with_table_names_opens_quietly(
    tmp_path: Path, adapter: type, old: str, new: str, table_names: TableNames
) -> None:
    """Either value silences the warning and creates only the picked table."""
    # Arrange
    async with SQLiteProvider(
        tmp_path / "app.db", table_names=table_names
    ) as provider:
        backend = adapter(provider=provider)

        # Act
        with _future_warnings() as caught:
            async with backend:
                pass

        # Assert
        assert caught == []
        async with provider.client.execute(
            "SELECT name FROM main.sqlite_master WHERE name IN (?, ?);",
            (old, new),
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == [
                old if table_names == "legacy" else new
            ]


@pytest.mark.parametrize(
    ("adapter", "old", "new"), _params(_POSTGRES + _SQLITE)
)
def test_a_store_table_name_wins_over_the_provider_table_names(
    adapter: type, old: str, new: str
) -> None:
    """A name passed to the store is kept, whatever the provider picks."""
    # Act
    built = adapter(
        provider=_provider_with(adapter, table_names="prefixed"),
        table_name="mine",
    )

    # Assert
    assert built._table_name == "mine"
    assert old != new


@pytest.mark.parametrize(
    ("provider_type", "prefix"),
    [(PostgresProvider, "POSTGRES_"), (SQLiteProvider, "SQLITE_")],
)
def test_table_names_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, provider_type: type, prefix: str
) -> None:
    """A provider left unset reads `{env_prefix}TABLE_NAMES`, in any case."""
    # Arrange
    monkeypatch.setenv(f"{prefix}TABLE_NAMES", "Legacy")
    target = _URL if provider_type is PostgresProvider else ":memory:"

    # Act
    from_env = provider_type(target)
    passed = provider_type(target, table_names="prefixed")
    no_env = provider_type(target, env_load=False)

    # Assert
    assert from_env.table_names == "legacy"
    assert passed.table_names == "prefixed"
    assert no_env.table_names is None


def test_a_store_that_builds_its_provider_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment pins every store, even one that builds its provider."""
    # Arrange
    monkeypatch.setenv("SQLITE_PATH", ":memory:")
    monkeypatch.setenv("SQLITE_TABLE_NAMES", "prefixed")

    # Act
    built = SQLiteLockAdapter()

    # Assert
    assert built._table_name == "grelmicro_locks"


@pytest.mark.parametrize(
    ("provider_type", "prefix"),
    [(PostgresProvider, "POSTGRES_"), (SQLiteProvider, "SQLITE_")],
)
def test_an_unknown_table_names_is_refused_without_echo(
    monkeypatch: pytest.MonkeyPatch, provider_type: type, prefix: str
) -> None:
    """A value other than `legacy` or `prefixed` is refused, never echoed."""
    # Arrange
    target = _URL if provider_type is PostgresProvider else ":memory:"
    monkeypatch.setenv(f"{prefix}TABLE_NAMES", "secret-env-value")

    # Act / Assert
    with pytest.raises(SettingsValidationError) as passed:
        provider_type(target, table_names="secret-kwarg-value", env_load=False)
    with pytest.raises(SettingsValidationError) as from_env:
        provider_type(target)

    assert "secret-kwarg-value" not in str(passed.value)
    assert "secret-env-value" not in str(from_env.value)


def test_table_names_carries_through_config_and_client() -> None:
    """`from_config`, `from_client` and `from_engine` keep `table_names`."""
    # Arrange
    pool = mock_pool()
    engine = create_async_engine("postgresql+asyncpg://u@localhost/db")

    # Act
    providers: list[PostgresProvider | SQLiteProvider] = [
        PostgresProvider.from_config(
            PostgresConfig(url=_URL, table_names="legacy")
        ),
        PostgresProvider.from_client(pool, table_names="legacy"),
        PostgresProvider.from_engine(engine, table_names="legacy"),
        SQLiteProvider.from_config(
            SQLiteConfig(path=":memory:", table_names="legacy")
        ),
        SQLiteProvider.from_client(AsyncMock(), table_names="legacy"),
    ]

    # Assert
    assert [provider.table_names for provider in providers] == ["legacy"] * 5
    assert PostgresProvider.from_client(pool).table_names is None


@pytest.fixture(scope="module")
def postgres_url() -> Generator[str]:
    """Start a Postgres container and yield its URL."""
    from testcontainers.community.postgres import (  # noqa: PLC0415
        PostgresContainer,
    )

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        yield f"postgresql://test:test@localhost:{port}/test"


async def _reset(url: str, *tables: str) -> None:
    """Drop `tables`, then create each table in `tables[1:]` empty."""
    conn = await asyncpg.connect(url)
    try:
        for table in tables:
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
        for table in tables[1:]:
            await conn.execute(f"CREATE TABLE {table} (name TEXT);")
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.timeout(60)
@pytest.mark.parametrize(("adapter", "old", "new"), _params(_POSTGRES))
async def test_postgres_opening_the_old_default_beside_the_new_table_warns(
    postgres_url: str, adapter: type, old: str, new: str
) -> None:
    """A rollback from 0.46 leaves both tables, and opening names both."""
    # Arrange
    await _reset(postgres_url, old, new)
    async with PostgresProvider(postgres_url) as provider:
        backend = adapter(provider=provider)

        # Act
        with _future_warnings() as caught:
            async with backend:
                pass

    # Assert
    assert [str(warning.message) for warning in caught] == [
        _default_message(adapter, old, new),
        _split_message(adapter, f"public.{old}", f"public.{new}"),
    ]
    assert {warning.filename for warning in caught} == {__file__}


@pytest.mark.integration
@pytest.mark.timeout(60)
@pytest.mark.parametrize(("adapter", "old", "new"), _params(_POSTGRES))
async def test_postgres_opening_the_old_default_alone_warns_once(
    postgres_url: str, adapter: type, old: str, new: str
) -> None:
    """Only the old table exists, so opening warns nothing more."""
    # Arrange
    await _reset(postgres_url, old)
    await _reset(postgres_url, new)
    async with PostgresProvider(postgres_url) as provider:
        backend = adapter(provider=provider)

        # Act
        with _future_warnings() as caught:
            async with backend:
                pass

        # Assert
        assert [str(warning.message) for warning in caught] == [
            _default_message(adapter, old, new)
        ]
        assert await provider.client.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", old
        )
        assert not await provider.client.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", new
        )
