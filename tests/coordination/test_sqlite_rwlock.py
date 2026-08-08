"""Test the SQLite read-write lock adapter paths outside the conformance suite."""

import pytest

from grelmicro.coordination.sqlite import SQLiteReadWriteLockAdapter
from grelmicro.providers.sqlite import SQLiteProvider

pytestmark = [pytest.mark.timeout(10, func_only=True)]


def test_table_name_must_be_an_identifier() -> None:
    """A table name that is not an identifier is refused at construction."""
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        SQLiteReadWriteLockAdapter(table_name="rwlocks; DROP TABLE users")


async def test_owns_its_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a provider the adapter builds and lifecycles its own."""
    monkeypatch.setenv("SQLITE_PATH", ":memory:")

    adapter = SQLiteReadWriteLockAdapter()

    async with adapter:
        assert adapter.provider.client is not None
        assert (
            await adapter.acquire_read(name="catalog", token="r", duration=10)
            == 0
        )


async def test_rebind_provider() -> None:
    """A rebound provider is borrowed rather than owned."""
    first = SQLiteProvider(":memory:")
    second = SQLiteProvider(":memory:")
    adapter = SQLiteReadWriteLockAdapter(provider=first)

    adapter._rebind_provider(second)

    assert adapter.provider is second


async def test_transaction_rolls_back_on_error() -> None:
    """A statement that fails rolls the transaction back and propagates."""
    provider = SQLiteProvider(":memory:")
    async with provider, SQLiteReadWriteLockAdapter(provider=provider) as rw:
        rw._sql["select_lock"] = "SELECT * FROM does_not_exist;"

        with pytest.raises(Exception, match="does_not_exist"):
            await rw.state(name="catalog")

        # The connection is usable again, so the rollback ran.
        assert await provider.client.execute("SELECT 1;") is not None
