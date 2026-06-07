"""Tests for MemoryCacheAdapter."""

from asyncio import sleep

import pytest

from grelmicro.cache.memory import MemoryCacheAdapter

pytestmark = [pytest.mark.timeout(5)]


class TestMemoryCacheAdapterGet:
    """Tests for MemoryCacheAdapter.get."""

    async def test_get_returns_stored_bytes(self) -> None:
        """Test that get returns the bytes stored by set."""
        backend = MemoryCacheAdapter()
        await backend.set(key="k", value=b"hello", ttl=60)

        result = await backend.get(key="k")

        assert result == b"hello"

    async def test_get_miss_returns_none(self) -> None:
        """Test that get returns None for a key that was never written."""
        backend = MemoryCacheAdapter()

        result = await backend.get(key="nonexistent")

        assert result is None

    async def test_get_expired_returns_none(self) -> None:
        """Test that get returns None after the entry's TTL has elapsed."""
        backend = MemoryCacheAdapter()
        await backend.set(key="exp", value=b"data", ttl=0.05)

        await sleep(0.1)

        result = await backend.get(key="exp")
        assert result is None

    async def test_get_removes_expired_entry_lazily(self) -> None:
        """Test that expired entries are removed from internal storage on access."""
        backend = MemoryCacheAdapter()
        await backend.set(key="lazy", value=b"val", ttl=0.05)

        await sleep(0.1)
        await backend.get(key="lazy")

        # After lazy removal, the internal dict must not contain the key.
        assert "lazy" not in backend._data

    async def test_get_not_yet_expired_returns_value(self) -> None:
        """Test that get returns the value when the TTL has not yet elapsed."""
        backend = MemoryCacheAdapter()
        await backend.set(key="fresh", value=b"still here", ttl=60)

        result = await backend.get(key="fresh")

        assert result == b"still here"

    async def test_get_empty_bytes_value(self) -> None:
        """Test that get correctly returns an empty bytes value."""
        backend = MemoryCacheAdapter()
        await backend.set(key="empty", value=b"", ttl=60)

        result = await backend.get(key="empty")

        assert result == b""

    async def test_get_large_value(self) -> None:
        """Test that get returns large byte payloads without truncation."""
        backend = MemoryCacheAdapter()
        large = b"x" * 100_000
        await backend.set(key="big", value=large, ttl=60)

        result = await backend.get(key="big")

        assert result == large

    async def test_get_unicode_key(self) -> None:
        """Test that get works with Unicode (non-ASCII) key strings."""
        backend = MemoryCacheAdapter()
        await backend.set(key="unicode-key", value=b"unicode-value", ttl=60)

        result = await backend.get(key="unicode-key")

        assert result == b"unicode-value"


class TestMemoryCacheAdapterSet:
    """Tests for MemoryCacheAdapter.set."""

    async def test_set_overwrites_existing_key(self) -> None:
        """Test that a second set replaces the previous value for the same key."""
        backend = MemoryCacheAdapter()
        await backend.set(key="k", value=b"first", ttl=60)
        await backend.set(key="k", value=b"second", ttl=60)

        result = await backend.get(key="k")

        assert result == b"second"

    async def test_set_updates_ttl_on_overwrite(self) -> None:
        """Test that overwriting a key resets its expiry to the new TTL."""
        backend = MemoryCacheAdapter()
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
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"aaa", ttl=60)
        await backend.set(key="b", value=b"bbb", ttl=60)

        assert await backend.get(key="a") == b"aaa"
        assert await backend.get(key="b") == b"bbb"


class TestMemoryCacheAdapterDelete:
    """Tests for MemoryCacheAdapter.delete."""

    async def test_delete_removes_key(self) -> None:
        """Test that delete makes the key unavailable."""
        backend = MemoryCacheAdapter()
        await backend.set(key="del", value=b"bye", ttl=60)

        await backend.delete(key="del")

        assert await backend.get(key="del") is None

    async def test_delete_missing_key_is_no_op(self) -> None:
        """Test that deleting a key that does not exist does not raise."""
        backend = MemoryCacheAdapter()

        # Should not raise.
        await backend.delete(key="ghost")

    async def test_delete_does_not_affect_other_keys(self) -> None:
        """Test that deleting one key leaves other keys intact."""
        backend = MemoryCacheAdapter()
        await backend.set(key="keep", value=b"safe", ttl=60)
        await backend.set(key="remove", value=b"gone", ttl=60)

        await backend.delete(key="remove")

        assert await backend.get(key="keep") == b"safe"
        assert await backend.get(key="remove") is None


class TestMemoryCacheAdapterClear:
    """Tests for MemoryCacheAdapter.clear."""

    async def test_clear_removes_all_entries(self) -> None:
        """Test that clear empties the store completely."""
        backend = MemoryCacheAdapter()
        await backend.set(key="one", value=b"1", ttl=60)
        await backend.set(key="two", value=b"2", ttl=60)

        await backend.clear()

        assert await backend.get(key="one") is None
        assert await backend.get(key="two") is None

    async def test_clear_on_empty_store_is_no_op(self) -> None:
        """Test that clearing an already-empty store does not raise."""
        backend = MemoryCacheAdapter()

        await backend.clear()

        assert await backend.get(key="any") is None

    async def test_can_set_after_clear(self) -> None:
        """Test that the backend remains usable after a clear."""
        backend = MemoryCacheAdapter()
        await backend.set(key="before", value=b"old", ttl=60)
        await backend.clear()

        await backend.set(key="after", value=b"new", ttl=60)
        result = await backend.get(key="after")

        assert result == b"new"


class TestMemoryCacheAdapterContextManager:
    """Tests for MemoryCacheAdapter async context manager."""

    async def test_context_manager_returns_self(self) -> None:
        """Test that __aenter__ returns the backend instance."""
        backend = MemoryCacheAdapter()

        async with backend as entered:
            assert entered is backend

    async def test_context_manager_clears_data_on_exit(self) -> None:
        """Test that __aexit__ clears the internal store."""
        backend = MemoryCacheAdapter()
        await backend.set(key="k", value=b"v", ttl=60)

        async with backend:
            pass

        # After the context manager exits, all data must be cleared.
        assert await backend.get(key="k") is None

    async def test_context_manager_clears_even_on_exception(self) -> None:
        """Test that __aexit__ clears the store even when the body raises."""
        backend = MemoryCacheAdapter()
        await backend.set(key="k", value=b"v", ttl=60)

        with pytest.raises(RuntimeError, match="test error"):  # noqa: PT012
            async with backend:
                msg = "test error"
                raise RuntimeError(msg)

        assert await backend.get(key="k") is None


class TestMemoryCacheAdapterTags:
    """Tests for tag membership in MemoryCacheAdapter."""

    async def test_set_with_tags_tracks_forward_and_reverse(self) -> None:
        """Test that set records both forward and reverse tag maps."""
        backend = MemoryCacheAdapter()

        await backend.set(key="k", value=b"v", ttl=60, tags=["t1", "t2"])

        assert backend._tag_keys["t1"] == {"k"}
        assert backend._tag_keys["t2"] == {"k"}
        assert backend._key_tags["k"] == {"t1", "t2"}

    async def test_set_without_tags_records_nothing(self) -> None:
        """Test that a tagless set leaves the tag maps empty."""
        backend = MemoryCacheAdapter()

        await backend.set(key="k", value=b"v", ttl=60)

        assert backend._tag_keys == {}
        assert backend._key_tags == {}

    async def test_set_overwrite_replaces_tags(self) -> None:
        """Test that re-setting a key drops its old tag membership."""
        backend = MemoryCacheAdapter()
        await backend.set(key="k", value=b"v", ttl=60, tags=["old"])

        await backend.set(key="k", value=b"v2", ttl=60, tags=["new"])

        assert "old" not in backend._tag_keys
        assert backend._tag_keys["new"] == {"k"}
        assert backend._key_tags["k"] == {"new"}

    async def test_set_overwrite_with_no_tags_clears_tags(self) -> None:
        """Test that re-setting a key with no tags clears its membership."""
        backend = MemoryCacheAdapter()
        await backend.set(key="k", value=b"v", ttl=60, tags=["t"])

        await backend.set(key="k", value=b"v2", ttl=60)

        assert backend._tag_keys == {}
        assert backend._key_tags == {}

    async def test_delete_removes_key_from_tags(self) -> None:
        """Test that delete cleans the key out of every tag it belonged to."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=60, tags=["shared"])
        await backend.set(key="b", value=b"b", ttl=60, tags=["shared"])

        await backend.delete(key="a")

        assert backend._tag_keys["shared"] == {"b"}
        assert "a" not in backend._key_tags

    async def test_delete_prunes_empty_tag(self) -> None:
        """Test that deleting the last member drops the tag entry."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=60, tags=["solo"])

        await backend.delete(key="a")

        assert "solo" not in backend._tag_keys

    async def test_lazy_expiry_cleans_tags(self) -> None:
        """Test that a lazily expired key is removed from its tags."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=0.05, tags=["t"])

        await sleep(0.1)
        assert await backend.get(key="a") is None

        assert "t" not in backend._tag_keys
        assert "a" not in backend._key_tags

    async def test_delete_tags_removes_all_members(self) -> None:
        """Test that delete_tags deletes every key under the tag."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=60, tags=["group"])
        await backend.set(key="b", value=b"b", ttl=60, tags=["group"])
        await backend.set(key="c", value=b"c", ttl=60, tags=["other"])

        await backend.delete_tags(tags=["group"])

        assert await backend.get(key="a") is None
        assert await backend.get(key="b") is None
        assert await backend.get(key="c") == b"c"
        assert "group" not in backend._tag_keys

    async def test_delete_tags_unknown_tag_is_no_op(self) -> None:
        """Test that deleting an unknown tag does not raise."""
        backend = MemoryCacheAdapter()

        await backend.delete_tags(tags=["ghost"])

    async def test_delete_tags_cascades_reverse_for_multitag_key(self) -> None:
        """Test that deleting via one tag also clears the key's other tags."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=60, tags=["t1", "t2"])

        await backend.delete_tags(tags=["t1"])

        assert await backend.get(key="a") is None
        assert "t2" not in backend._tag_keys
        assert "a" not in backend._key_tags

    async def test_clear_resets_tag_maps(self) -> None:
        """Test that clear empties the tag maps too."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=60, tags=["t"])

        await backend.clear()

        assert backend._tag_keys == {}
        assert backend._key_tags == {}


class TestMemoryCacheAdapterBatch:
    """Tests for batch operations in MemoryCacheAdapter."""

    async def test_get_many_returns_found_only(self) -> None:
        """Test that get_many omits missing keys."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=60)
        await backend.set(key="b", value=b"b", ttl=60)

        result = await backend.get_many(keys=["a", "b", "missing"])

        assert result == {"a": b"a", "b": b"b"}

    async def test_get_many_drops_expired(self) -> None:
        """Test that get_many lazily drops an expired key."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=0.05, tags=["t"])
        await backend.set(key="b", value=b"b", ttl=60)

        await sleep(0.1)
        result = await backend.get_many(keys=["a", "b"])

        assert result == {"b": b"b"}
        assert "a" not in backend._data
        assert "t" not in backend._tag_keys

    async def test_set_many_stores_all_with_tags(self) -> None:
        """Test that set_many writes every key and associates tags."""
        backend = MemoryCacheAdapter()

        await backend.set_many(items={"a": b"a", "b": b"b"}, ttl=60, tags=["g"])

        assert await backend.get(key="a") == b"a"
        assert await backend.get(key="b") == b"b"
        assert backend._tag_keys["g"] == {"a", "b"}

    async def test_delete_many_removes_all(self) -> None:
        """Test that delete_many deletes every listed key."""
        backend = MemoryCacheAdapter()
        await backend.set(key="a", value=b"a", ttl=60, tags=["t"])
        await backend.set(key="b", value=b"b", ttl=60)

        await backend.delete_many(keys=["a", "b", "missing"])

        assert await backend.get(key="a") is None
        assert await backend.get(key="b") is None
        assert "t" not in backend._tag_keys
