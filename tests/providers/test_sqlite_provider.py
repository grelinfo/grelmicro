"""Tests for the SQLite Provider."""

from pathlib import Path

import aiosqlite
import pytest

from grelmicro.cache.sqlite import SQLiteCacheAdapter
from grelmicro.coordination.sqlite import (
    SQLiteLockAdapter,
    SQLiteScheduleAdapter,
)
from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.providers.sqlite import SQLiteConfig, SQLiteProvider
from grelmicro.resilience.circuitbreaker.sqlite import (
    SQLiteCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.sqlite import SQLiteRateLimiterAdapter


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
    with pytest.raises(OutOfContextError):
        _ = SQLiteProvider("x.db").client


def test_factories_return_adapters() -> None:
    """The provider builds an adapter for every supported component."""
    provider = SQLiteProvider("x.db")
    assert isinstance(provider.ratelimiter(), SQLiteRateLimiterAdapter)
    assert isinstance(provider.lock(), SQLiteLockAdapter)
    assert isinstance(provider.schedule(), SQLiteScheduleAdapter)
    assert isinstance(provider.cache(), SQLiteCacheAdapter)
    assert isinstance(provider.circuitbreaker(), SQLiteCircuitBreakerAdapter)


async def test_open_and_close(tmp_path: Path) -> None:
    """The provider opens a connection on enter and closes it on exit."""
    provider = SQLiteProvider(tmp_path / "app.db")
    async with provider as opened:
        assert opened.client is not None
        assert isinstance(opened.connection_lock.locked(), bool)
    with pytest.raises(OutOfContextError):
        _ = provider.client


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
