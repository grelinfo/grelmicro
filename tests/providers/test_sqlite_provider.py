"""Tests for the SQLite Provider."""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import anyio
import pytest
from pytest_mock import MockerFixture

from grelmicro import Grelmicro
from grelmicro.cache.sqlite import SQLiteCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination.sqlite import (
    SQLiteLeaderElectionAdapter,
    SQLiteLockAdapter,
    SQLiteScheduleAdapter,
)
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.providers.sqlite import (
    SQLiteConfig,
    SQLiteProvider,
    _enable_wal,
)
from grelmicro.resilience.circuitbreaker.sqlite import (
    SQLiteCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.sqlite import SQLiteRateLimiterAdapter
from grelmicro.task import Tasks

BUSY_TIMEOUT_MS = 5000
"""Milliseconds a write waits for a file another process is writing."""

WAL_ATTEMPTS_WHEN_LOCKED = 2
"""Attempts the WAL switch takes when the first one finds the file locked."""

_tasks = Tasks()


def _busy_error() -> sqlite3.OperationalError:
    """Build the error SQLite raises when another connection holds the file."""
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


class _FakeCursor:
    """Cursor holding the one row a PRAGMA returns."""

    def __init__(self, row: tuple[str]) -> None:
        self._row = row

    async def fetchone(self) -> tuple[str]:
        return self._row

    async def fetchall(self) -> list[tuple[str]]:
        return [self._row]


class _FakeConnection:
    """Connection on a file in `mode` whose WAL switch fails on demand.

    Each attempt at the switch takes the next item of `switch_outcomes`:
    an exception is raised, `None` lets the switch run and leaves the file
    in `switched_mode`.
    """

    def __init__(
        self,
        mode: str,
        switch_outcomes: list[BaseException | None] | None = None,
        *,
        switched_mode: str = "wal",
    ) -> None:
        self._mode = mode
        self._switched_mode = switched_mode
        self._outcomes: Iterator[BaseException | None] = iter(
            switch_outcomes or [None]
        )
        self.statements: list[str] = []

    @asynccontextmanager
    async def _run(self, sql: str) -> AsyncGenerator[_FakeCursor]:
        if sql == "PRAGMA journal_mode=WAL;":
            outcome = next(self._outcomes)
            if outcome is not None:
                raise outcome
            self._mode = self._switched_mode
        yield _FakeCursor((self._mode,))

    def execute(self, sql: str) -> Any:  # noqa: ANN401
        self.statements.append(sql)
        return self._run(sql)


@_tasks.every(
    interval=timedelta(milliseconds=50),
    gate=TaskLock(
        lease_duration=2, min_hold_duration=timedelta(milliseconds=50)
    ),
)
async def _locked_job() -> None:
    """Interval task gated by a distributed lock on the shared file."""


def test_positional_path() -> None:
    """A positional path is stored on the provider."""
    provider = SQLiteProvider("app.db")
    assert provider.path == "app.db"
    assert provider.env_prefix == "SQLITE_"


def test_env_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a path, the provider reads `SQLITE_PATH`."""
    monkeypatch.setenv("SQLITE_PATH", "/tmp/env.db")  # noqa: S108
    provider = SQLiteProvider()
    assert provider.path == "/tmp/env.db"  # noqa: S108


def test_missing_path_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No path and no env raises a settings error."""
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    with pytest.raises(SettingsValidationError, match="SQLITE_PATH"):
        SQLiteProvider()


def test_env_load_false_ignores_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=False` does not read the environment."""
    monkeypatch.setenv("SQLITE_PATH", "/tmp/env.db")  # noqa: S108
    with pytest.raises(SettingsValidationError):
        SQLiteProvider(env_load=False)


def test_from_config() -> None:
    """`from_config` builds a provider from a `SQLiteConfig`."""
    provider = SQLiteProvider.from_config(SQLiteConfig(path="cfg.db"))
    assert provider.path == "cfg.db"


def test_repr_carries_path() -> None:
    """`repr` shows the path."""
    assert "cfg.db" in repr(SQLiteProvider("cfg.db"))


def test_client_before_open_raises() -> None:
    """Accessing the client before `__aenter__` raises."""
    with pytest.raises(OutOfContextError) as info:
        _ = SQLiteProvider("x.db").client
    assert str(info.value) == (
        "Could not call SQLiteProvider.client: the SQLiteProvider is not "
        "open. It was never opened, or it already closed. Register it in "
        "Grelmicro(uses=[...]) so the app opens it, or make the call while "
        "it is open."
    )


def test_factories_return_adapters() -> None:
    """The provider builds an adapter for every supported component."""
    provider = SQLiteProvider("x.db")
    assert isinstance(provider.ratelimiter_backend(), SQLiteRateLimiterAdapter)
    assert isinstance(provider.lock_backend(), SQLiteLockAdapter)
    assert isinstance(
        provider.leaderelection_backend(), SQLiteLeaderElectionAdapter
    )
    assert isinstance(provider.schedule_backend(), SQLiteScheduleAdapter)
    assert isinstance(provider.cache_backend(), SQLiteCacheAdapter)
    assert isinstance(
        provider.circuitbreaker_backend(), SQLiteCircuitBreakerAdapter
    )


async def test_open_and_close(tmp_path: Path) -> None:
    """The provider opens a connection on enter and closes it on exit."""
    provider = SQLiteProvider(tmp_path / "app.db")
    async with provider as opened:
        assert opened.client is not None
        assert isinstance(opened.connection_lock.locked(), bool)
    with pytest.raises(OutOfContextError):
        _ = provider.client


async def test_aenter_closes_connection_when_pragma_fails(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """A failing WAL PRAGMA closes the opened connection instead of leaking it."""
    # Arrange: connect succeeds but the PRAGMA execute raises.
    conn = AsyncMock()
    conn.execute = MagicMock(
        side_effect=aiosqlite.OperationalError("disk I/O error")
    )
    mocker.patch(
        "grelmicro.providers.sqlite.aiosqlite.connect",
        new=AsyncMock(return_value=conn),
    )
    provider = SQLiteProvider(tmp_path / "wal.db")

    # Act
    with pytest.raises(aiosqlite.OperationalError, match="disk I/O error"):
        await provider.__aenter__()

    # Assert: the open connection was closed, not leaked.
    conn.close.assert_awaited_once()


async def test_from_client_does_not_own(tmp_path: Path) -> None:
    """`from_client` borrows the connection and leaves it open on exit."""
    conn = await aiosqlite.connect(tmp_path / "byo.db", isolation_level=None)
    try:
        provider = SQLiteProvider.from_client(conn)
        async with provider:
            assert provider.client is conn
        # Not owned: still usable after exit.
        await conn.execute("SELECT 1;")
    finally:
        await conn.close()


async def test_from_client_owns_when_requested(tmp_path: Path) -> None:
    """`from_client(own=True)` closes the connection on exit."""
    conn = await aiosqlite.connect(tmp_path / "owned.db", isolation_level=None)
    provider = SQLiteProvider.from_client(conn, own=True)
    async with provider:
        assert provider.client is conn
    # Owned: closed on exit, so the client is no longer available.
    with pytest.raises(OutOfContextError):
        _ = provider.client


async def test_check_runs_select_one(tmp_path: Path) -> None:
    """`check` runs `SELECT 1` on the connection and returns None."""
    provider = SQLiteProvider(tmp_path / "check.db")
    async with provider:
        assert await provider.check() is None


async def test_open_sets_busy_timeout_and_wal(tmp_path: Path) -> None:
    """An opened connection waits on a busy file and journals with WAL."""
    # Arrange / Act
    async with SQLiteProvider(tmp_path / "pragmas.db") as provider:
        async with provider.client.execute("PRAGMA busy_timeout;") as cursor:
            busy_timeout = await cursor.fetchone()
        async with provider.client.execute("PRAGMA journal_mode;") as cursor:
            journal_mode = await cursor.fetchone()

    # Assert
    assert busy_timeout is not None
    assert busy_timeout[0] == BUSY_TIMEOUT_MS
    assert journal_mode is not None
    assert journal_mode[0] == "wal"


async def test_enable_wal_retries_while_the_file_is_locked() -> None:
    """The WAL switch is retried while another process holds the file."""
    # Arrange: the first attempt finds the file locked, the second succeeds.
    conn = _FakeConnection("delete", [_busy_error(), None])

    # Act
    await _enable_wal(conn, "app.db")  # ty: ignore[invalid-argument-type]

    # Assert
    attempts = conn.statements.count("PRAGMA journal_mode=WAL;")
    assert attempts == WAL_ATTEMPTS_WHEN_LOCKED


async def test_enable_wal_raises_when_the_file_stays_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file locked past the deadline surfaces the error instead of hanging."""
    # Arrange: no time left, so the first failure is the last.
    monkeypatch.setattr("grelmicro.providers.sqlite._WAL_SWITCH_TIMEOUT", 0.0)
    conn = _FakeConnection("delete", [_busy_error()])

    # Act / Assert
    with pytest.raises(aiosqlite.OperationalError, match="database is locked"):
        await _enable_wal(conn, "app.db")  # ty: ignore[invalid-argument-type]


async def test_enable_wal_raises_other_errors_at_once() -> None:
    """An error other than a busy file is raised on the first attempt."""
    # Arrange
    error = sqlite3.OperationalError("attempt to write a readonly database")
    error.sqlite_errorcode = sqlite3.SQLITE_READONLY
    conn = _FakeConnection("delete", [error, None])

    # Act / Assert
    with pytest.raises(aiosqlite.OperationalError, match="readonly"):
        await _enable_wal(conn, "app.db")  # ty: ignore[invalid-argument-type]
    assert conn.statements.count("PRAGMA journal_mode=WAL;") == 1


async def test_enable_wal_reads_the_journal_mode_first() -> None:
    """The journal mode is read before the switch is attempted."""
    # Arrange
    conn = _FakeConnection("delete")

    # Act
    await _enable_wal(conn, "app.db")  # ty: ignore[invalid-argument-type]

    # Assert
    assert conn.statements == [
        "PRAGMA journal_mode;",
        "PRAGMA journal_mode=WAL;",
    ]


async def test_second_provider_on_a_wal_file_skips_the_switch(
    tmp_path: Path,
) -> None:
    """A file already in WAL is opened without running the WAL switch."""
    # Arrange: a first provider leaves the file in WAL.
    path = tmp_path / "wal.db"
    async with SQLiteProvider(path):
        pass
    conn = await aiosqlite.connect(path, isolation_level=None)
    statements: list[str] = []
    try:
        await conn.set_trace_callback(statements.append)

        # Act
        await _enable_wal(conn, str(path))
    finally:
        await conn.close()

    # Assert
    assert statements == ["PRAGMA journal_mode;"]


@pytest.mark.parametrize(
    "build_adapter",
    [
        pytest.param(lambda provider: provider.cache_backend(), id="cache"),
        pytest.param(
            lambda provider: provider.ratelimiter_backend(), id="ratelimiter"
        ),
        pytest.param(
            lambda provider: provider.circuitbreaker_backend(),
            id="circuitbreaker",
        ),
    ],
)
async def test_schema_init_waits_for_the_shared_connection(
    tmp_path: Path,
    build_adapter: Callable[[SQLiteProvider], Any],
) -> None:
    """Creating tables waits for whoever holds the shared connection.

    Another component may have a transaction open on it, and a schema init
    that ran anyway would end that transaction and fail.
    """
    # Arrange: hold the connection the way a transaction in progress does.
    async with SQLiteProvider(tmp_path / "shared.db") as provider:
        adapter = build_adapter(provider)
        await provider.connection_lock.acquire()
        task = asyncio.create_task(adapter.__aenter__())
        await asyncio.sleep(0.05)

        # Assert: the schema init is still waiting for the lock.
        assert not task.done()

        # Act: let the holder go.
        provider.connection_lock.release()
        await asyncio.wait_for(task, timeout=5)

        # Assert
        assert task.done()
        await adapter.__aexit__(None, None, None)


async def test_cache_and_coordination_share_one_file(tmp_path: Path) -> None:
    """An app wiring a bare provider and a `Coordination` on one file starts.

    The bare provider registers a default `Cache`, so the cache schema init
    and the coordination schema init run against the same connection while a
    task acquires its lock.
    """
    # Arrange
    sqlite = SQLiteProvider(tmp_path / "shared.db")

    # Act / Assert: the app opens and closes without raising.
    async with Grelmicro(
        uses=[sqlite, Coordination(sqlite, requires="host"), _tasks]
    ):
        await anyio.sleep(0.2)


async def test_check_propagates_failure(tmp_path: Path) -> None:
    """A query failure on the connection surfaces from `check`."""
    conn = await aiosqlite.connect(tmp_path / "byo.db", isolation_level=None)
    provider = SQLiteProvider.from_client(conn)  # not owned: we close it
    async with provider:
        await conn.close()  # force the next query to fail
        with pytest.raises(Exception, match=r"(?i)connection"):
            await provider.check()


@pytest.fixture
def short_busy_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a locked file fail within a fraction of a second, not hang."""
    monkeypatch.setattr("grelmicro.providers.sqlite._BUSY_TIMEOUT_MS", 200)
    monkeypatch.setattr("grelmicro.providers.sqlite._WAL_SWITCH_TIMEOUT", 0.2)


@pytest.mark.usefixtures("short_busy_wait")
async def test_two_providers_on_one_file_do_not_block_each_other(
    tmp_path: Path,
) -> None:
    """A second provider on a file another provider holds opens and writes."""
    # Arrange
    path = tmp_path / "shared.db"
    first = SQLiteProvider(path)
    second = SQLiteProvider(path)

    # Act
    async with first, second:
        await second.check()
        async with second.cache_backend() as cache:
            await cache.set(key="k", value=b"v", ttl=timedelta(seconds=60))
            value = await cache.get(key="k")

    # Assert
    assert value == b"v"


@pytest.mark.parametrize("switched_mode", ["wal", "memory"])
async def test_enable_wal_is_silent_when_the_switch_lands(
    caplog: pytest.LogCaptureFixture, switched_mode: str
) -> None:
    """A switch that returns WAL, or memory for an in-memory database, logs nothing."""
    # Arrange
    conn = _FakeConnection("delete", switched_mode=switched_mode)

    # Act
    with caplog.at_level("WARNING", logger="grelmicro.providers"):
        await _enable_wal(conn, "app.db")  # ty: ignore[invalid-argument-type]

    # Assert
    assert caplog.records == []


async def test_enable_wal_warns_when_sqlite_keeps_the_old_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A switch SQLite refuses silently logs one warning with path and mode."""
    # Arrange
    conn = _FakeConnection("delete", switched_mode="delete")

    # Act
    with caplog.at_level("WARNING", logger="grelmicro.providers"):
        await _enable_wal(conn, "/mnt/share/app.db")  # ty: ignore[invalid-argument-type]

    # Assert
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "'/mnt/share/app.db'" in message
    assert "'delete'" in message


async def test_in_memory_provider_opens_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An in-memory database opens without the WAL warning."""
    # Act
    with caplog.at_level("WARNING", logger="grelmicro.providers"):
        async with SQLiteProvider(":memory:") as provider:
            await provider.check()

    # Assert
    assert caplog.records == []
