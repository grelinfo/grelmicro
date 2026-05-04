"""Tests for MemoryCacheBackend."""

from asyncio import sleep

import pytest

from grelmicro._backends import BackendNotLoadedError
from grelmicro.cache import use_backend
from grelmicro.cache._backends import cache_backend_registry, get_cache_backend
from grelmicro.cache.memory import MemoryCacheBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(5)]


class TestMemoryCacheBackendGet:
    """Tests for MemoryCacheBackend.get."""

    async def test_get_returns_stored_bytes(self) -> None:
        """Test that get returns the bytes stored by set."""
        backend = MemoryCacheBackend()
        await backend.set(key="k", value=b"hello", ttl=60)

        result = await backend.get(key="k")

        assert result == b"hello"

    async def test_get_miss_returns_none(self) -> None:
        """Test that get returns None for a key that was never written."""
        backend = MemoryCacheBackend()

        result = await backend.get(key="nonexistent")

        assert result is None

    async def test_get_expired_returns_none(self) -> None:
        """Test that get returns None after the entry's TTL has elapsed."""
        backend = MemoryCacheBackend()
        await backend.set(key="exp", value=b"data", ttl=0.05)

        await sleep(0.1)

        result = await backend.get(key="exp")
        assert result is None

    async def test_get_removes_expired_entry_lazily(self) -> None:
        """Test that expired entries are removed from internal storage on access."""
        backend = MemoryCacheBackend()
        await backend.set(key="lazy", value=b"val", ttl=0.05)

        await sleep(0.1)
        await backend.get(key="lazy")

        # After lazy removal, the internal dict must not contain the key.
        assert "lazy" not in backend._data

    async def test_get_not_yet_expired_returns_value(self) -> None:
        """Test that get returns the value when the TTL has not yet elapsed."""
        backend = MemoryCacheBackend()
        await backend.set(key="fresh", value=b"still here", ttl=60)

        result = await backend.get(key="fresh")

        assert result == b"still here"

    async def test_get_empty_bytes_value(self) -> None:
        """Test that get correctly returns an empty bytes value."""
        backend = MemoryCacheBackend()
        await backend.set(key="empty", value=b"", ttl=60)

        result = await backend.get(key="empty")

        assert result == b""

    async def test_get_large_value(self) -> None:
        """Test that get returns large byte payloads without truncation."""
        backend = MemoryCacheBackend()
        large = b"x" * 100_000
        await backend.set(key="big", value=large, ttl=60)

        result = await backend.get(key="big")

        assert result == large

    async def test_get_unicode_key(self) -> None:
        """Test that get works with Unicode (non-ASCII) key strings."""
        backend = MemoryCacheBackend()
        await backend.set(key="unicode-key", value=b"unicode-value", ttl=60)

        result = await backend.get(key="unicode-key")

        assert result == b"unicode-value"


class TestMemoryCacheBackendSet:
    """Tests for MemoryCacheBackend.set."""

    async def test_set_overwrites_existing_key(self) -> None:
        """Test that a second set replaces the previous value for the same key."""
        backend = MemoryCacheBackend()
        await backend.set(key="k", value=b"first", ttl=60)
        await backend.set(key="k", value=b"second", ttl=60)

        result = await backend.get(key="k")

        assert result == b"second"

    async def test_set_updates_ttl_on_overwrite(self) -> None:
        """Test that overwriting a key resets its expiry to the new TTL."""
        backend = MemoryCacheBackend()
        # Write with a very short TTL.
        await backend.set(key="k", value=b"v", ttl=0.05)
        # Immediately overwrite with a long TTL.
        await backend.set(key="k", value=b"v2", ttl=60)

        # Wait past the original short TTL.
        await sleep(0.1)

        # The new TTL is long, so the key must still be present.
        result = await backend.get(key="k")
        assert result == b"v2"

    async def test_set_multiple_keys_independently(self) -> None:
        """Test that multiple keys do not interfere with each other."""
        backend = MemoryCacheBackend()
        await backend.set(key="a", value=b"aaa", ttl=60)
        await backend.set(key="b", value=b"bbb", ttl=60)

        assert await backend.get(key="a") == b"aaa"
        assert await backend.get(key="b") == b"bbb"


class TestMemoryCacheBackendDelete:
    """Tests for MemoryCacheBackend.delete."""

    async def test_delete_removes_key(self) -> None:
        """Test that delete makes the key unavailable."""
        backend = MemoryCacheBackend()
        await backend.set(key="del", value=b"bye", ttl=60)

        await backend.delete(key="del")

        assert await backend.get(key="del") is None

    async def test_delete_missing_key_is_no_op(self) -> None:
        """Test that deleting a key that does not exist does not raise."""
        backend = MemoryCacheBackend()

        # Should not raise.
        await backend.delete(key="ghost")

    async def test_delete_does_not_affect_other_keys(self) -> None:
        """Test that deleting one key leaves other keys intact."""
        backend = MemoryCacheBackend()
        await backend.set(key="keep", value=b"safe", ttl=60)
        await backend.set(key="remove", value=b"gone", ttl=60)

        await backend.delete(key="remove")

        assert await backend.get(key="keep") == b"safe"
        assert await backend.get(key="remove") is None


class TestMemoryCacheBackendClear:
    """Tests for MemoryCacheBackend.clear."""

    async def test_clear_removes_all_entries(self) -> None:
        """Test that clear empties the store completely."""
        backend = MemoryCacheBackend()
        await backend.set(key="one", value=b"1", ttl=60)
        await backend.set(key="two", value=b"2", ttl=60)

        await backend.clear()

        assert await backend.get(key="one") is None
        assert await backend.get(key="two") is None

    async def test_clear_on_empty_store_is_no_op(self) -> None:
        """Test that clearing an already-empty store does not raise."""
        backend = MemoryCacheBackend()

        await backend.clear()

        assert await backend.get(key="any") is None

    async def test_can_set_after_clear(self) -> None:
        """Test that the backend remains usable after a clear."""
        backend = MemoryCacheBackend()
        await backend.set(key="before", value=b"old", ttl=60)
        await backend.clear()

        await backend.set(key="after", value=b"new", ttl=60)
        result = await backend.get(key="after")

        assert result == b"new"


class TestMemoryCacheBackendContextManager:
    """Tests for MemoryCacheBackend async context manager."""

    async def test_context_manager_returns_self(self) -> None:
        """Test that __aenter__ returns the backend instance."""
        backend = MemoryCacheBackend()

        async with backend as entered:
            assert entered is backend

    async def test_context_manager_clears_data_on_exit(self) -> None:
        """Test that __aexit__ clears the internal store."""
        backend = MemoryCacheBackend()
        await backend.set(key="k", value=b"v", ttl=60)

        async with backend:
            pass

        # After the context manager exits, all data must be cleared.
        assert await backend.get(key="k") is None

    async def test_context_manager_clears_even_on_exception(self) -> None:
        """Test that __aexit__ clears the store even when the body raises."""
        backend = MemoryCacheBackend()
        await backend.set(key="k", value=b"v", ttl=60)

        with pytest.raises(RuntimeError, match="test error"):  # noqa: PT012
            async with backend:
                msg = "test error"
                raise RuntimeError(msg)

        assert await backend.get(key="k") is None


class TestMemoryCacheBackendRegistration:
    """Tests for explicit registration of MemoryCacheBackend."""

    def test_constructor_does_not_register(self) -> None:
        """Constructing the backend performs no registry writes."""
        cache_backend_registry.reset()

        MemoryCacheBackend()

        assert not cache_backend_registry.is_loaded

    def test_use_backend_registers(self) -> None:
        """`cache.use_backend` registers the backend as default."""
        cache_backend_registry.reset()
        backend = MemoryCacheBackend()

        use_backend(backend)

        assert get_cache_backend() is backend

        cache_backend_registry.reset()

    def test_get_cache_backend_not_loaded_raises(self) -> None:
        """Test that get_cache_backend raises BackendNotLoadedError when empty."""
        cache_backend_registry.reset()

        with pytest.raises(BackendNotLoadedError):
            get_cache_backend()
