"""Tests for the SQLite-specific cache adapter paths."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from grelmicro.cache.sqlite import SQLiteCacheAdapter
from grelmicro.providers.sqlite import SQLiteProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

pytestmark = [pytest.mark.timeout(5)]


# --- Construction and wiring ---


def test_invalid_table_name_raises() -> None:
    """An invalid SQL identifier for the table name raises."""
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        SQLiteCacheAdapter(table_name="bad name;")


@pytest.mark.parametrize("interval", [0, -1.0])
def test_non_positive_cleanup_interval_raises(interval: float) -> None:
    """A non-positive `cleanup_interval` is rejected."""
    with pytest.raises(ValueError, match="cleanup_interval must be positive"):
        SQLiteCacheAdapter(cleanup_interval=interval)


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("SQLITE_PATH", "/tmp/cache.db")  # noqa: S108

    adapter = SQLiteCacheAdapter()

    assert adapter.provider.path == "/tmp/cache.db"  # noqa: S108
    assert adapter._owns_provider is True


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is exposed and not owned."""
    provider = SQLiteProvider("x.db")

    adapter = SQLiteCacheAdapter(provider=provider)

    assert adapter.provider is provider
    assert adapter._owns_provider is False


def test_rebind_provider_borrows_it() -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    adapter = SQLiteCacheAdapter(provider=SQLiteProvider("a.db"))
    other = SQLiteProvider("b.db")

    adapter._rebind_provider(other)

    assert adapter.provider is other
    assert adapter._owns_provider is False


# --- Lifecycle ---


async def test_owned_provider_is_opened_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When owned, the adapter opens and closes its provider itself."""
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "owned.db"))
    cache = SQLiteCacheAdapter()
    assert cache._owns_provider is True

    async with cache:
        await cache.set(key="k", value=b"v", ttl=60)
        assert await cache.get(key="k") == b"v"

    with pytest.raises(Exception, match="outside of the context manager"):
        _ = cache.provider.client


async def test_auto_migrate_false_skips_schema(tmp_path: Path) -> None:
    """`auto_migrate=False` leaves the schema to the caller."""
    path = tmp_path / "no_migrate.db"
    async with (
        SQLiteProvider(str(path)) as provider,
        SQLiteCacheAdapter(provider=provider, auto_migrate=False) as cache,
    ):
        with pytest.raises(Exception, match="no such table"):
            await cache.get(key="k")


# --- Janitor ---


async def test_janitor_reclaims_expired_rows(tmp_path: Path) -> None:
    """The janitor deletes rows expired for more than one hour."""
    path = tmp_path / "janitor.db"
    async with (
        SQLiteProvider(str(path)) as provider,
        SQLiteCacheAdapter(provider=provider, cleanup_interval=0.01) as cache,
    ):
        await cache.set(key="stale", value=b"v", ttl=-7200)
        conn = provider.client
        for _ in range(50):
            await asyncio.sleep(0.02)
            async with conn.execute(
                "SELECT COUNT(*) FROM grelmicro_cache;"
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None and row[0] == 0:
                break
        assert row is not None
        assert row[0] == 0


async def test_janitor_stops_on_exit(tmp_path: Path) -> None:
    """The janitor task is cancelled and cleared on exit."""
    path = tmp_path / "janitor_exit.db"
    async with SQLiteProvider(str(path)) as provider:
        cache = SQLiteCacheAdapter(provider=provider, cleanup_interval=0.01)
        async with cache:
            assert cache._janitor_task is not None
        assert cache._janitor_task is None


# --- Rollback paths ---


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncGenerator[SQLiteCacheAdapter]:
    """Open a SQLite cache adapter on a temp file."""
    async with (
        SQLiteProvider(str(tmp_path / "cache.db")) as provider,
        SQLiteCacheAdapter(provider=provider) as adapter,
    ):
        yield adapter


async def test_set_rolls_back_on_error(cache: SQLiteCacheAdapter) -> None:
    """A failing `set` rolls back the open transaction and re-raises."""
    conn = cache.provider.client
    await conn.execute("DROP TABLE grelmicro_cache;")

    with pytest.raises(Exception, match="no such table"):
        await cache.set(key="k", value=b"v", ttl=60)

    assert conn.in_transaction is False


async def test_set_many_rolls_back_on_error(cache: SQLiteCacheAdapter) -> None:
    """A failing `set_many` rolls back the open transaction and re-raises."""
    conn = cache.provider.client
    await conn.execute("DROP TABLE grelmicro_cache;")

    with pytest.raises(Exception, match="no such table"):
        await cache.set_many(items={"k": b"v"}, ttl=60)

    assert conn.in_transaction is False


async def test_delete_tags_empty_is_no_op(cache: SQLiteCacheAdapter) -> None:
    """`delete_tags` with no tags does nothing."""
    await cache.delete_tags(tags=[])


async def test_delete_tags_rolls_back_on_error(
    cache: SQLiteCacheAdapter,
) -> None:
    """A failing `delete_tags` rolls back and re-raises."""
    conn = cache.provider.client
    await conn.execute("DROP TABLE grelmicro_cache;")

    with pytest.raises(Exception, match="no such table"):
        await cache.delete_tags(tags=["g"])

    assert conn.in_transaction is False


async def test_clear_without_prefix_drops_everything(
    tmp_path: Path,
) -> None:
    """With no prefix, `clear` removes every row via the full-table delete."""
    path = tmp_path / "clear_all.db"
    async with (
        SQLiteProvider(str(path)) as provider,
        SQLiteCacheAdapter(provider=provider) as cache,
    ):
        await cache.set(key="a", value=b"a", ttl=60)
        await cache.set(key="b", value=b"b", ttl=60)

        await cache.clear()

        assert await cache.get(key="a") is None
        assert await cache.get(key="b") is None
