"""Memory Cache Adapter."""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from time import monotonic
from types import TracebackType
from typing import Self

from grelmicro.cache._protocol import CacheBackend


class MemoryCacheAdapter(CacheBackend):
    """In-memory cache backend.

    Stores entries in a Python dict with lazy TTL expiry.
    Suitable for testing and single-process applications.

    Tag membership is tracked with two dicts kept in sync: a forward
    map from tag to its set of keys, and a reverse map from key to its
    set of tags. The reverse map lets a delete or a lazy expiry remove
    the key from every tag it belonged to, so no tag ever points at a
    key that is gone.
    """

    def __init__(self) -> None:
        """Initialize the memory cache backend."""
        self._data: dict[str, tuple[bytes, float]] = {}
        self._tag_keys: dict[str, set[str]] = {}
        self._key_tags: dict[str, set[str]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        """Open the cache backend."""
        self._loop = asyncio.get_running_loop()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the cache backend."""
        self._data.clear()
        self._tag_keys.clear()
        self._key_tags.clear()

    async def get(self, *, key: str) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        Expired entries are removed lazily on access.
        """
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if monotonic() >= expiry:
            self._drop(key)
            return None
        return value

    async def set(
        self,
        *,
        key: str,
        value: bytes,
        ttl: float,
        tags: Sequence[str] = (),
    ) -> None:
        """Store raw bytes with a TTL in seconds and optional tags."""
        self._data[key] = (value, monotonic() + ttl)
        self._associate(key, tags)

    async def get_many(self, *, keys: Sequence[str]) -> dict[str, bytes]:
        """Get raw bytes for many keys, returning only found entries."""
        now = monotonic()
        found: dict[str, bytes] = {}
        for key in keys:
            entry = self._data.get(key)
            if entry is None:
                continue
            value, expiry = entry
            if now >= expiry:
                self._drop(key)
                continue
            found[key] = value
        return found

    async def set_many(
        self,
        *,
        items: Mapping[str, bytes],
        ttl: float,
        tags: Sequence[str] = (),
    ) -> None:
        """Store many keys with one TTL and optional tags."""
        expiry = monotonic() + ttl
        for key, value in items.items():
            self._data[key] = (value, expiry)
            self._associate(key, tags)

    async def delete(self, *, key: str) -> None:
        """Delete a key and clean its tag membership (no-op if absent)."""
        self._drop(key)

    async def delete_many(self, *, keys: Sequence[str]) -> None:
        """Delete many keys and clean their tag membership."""
        for key in keys:
            self._drop(key)

    async def delete_tags(self, *, tags: Sequence[str]) -> None:
        """Delete every key associated with any of the given tags."""
        for tag in tags:
            members = self._tag_keys.get(tag)
            if members is None:
                continue
            for key in list(members):
                self._drop(key)
            self._tag_keys.pop(tag, None)

    async def clear(self) -> None:
        """Remove all entries."""
        self._data.clear()
        self._tag_keys.clear()
        self._key_tags.clear()

    def _associate(self, key: str, tags: Sequence[str]) -> None:
        """Record tag membership for a freshly written key.

        Tags from a previous write of the same key are first cleared so
        the membership reflects only the latest write.
        """
        old = self._key_tags.pop(key, None)
        if old is not None:
            self._remove_from_tags(key, old)
        if not tags:
            return
        new = set(tags)
        self._key_tags[key] = new
        for tag in new:
            self._tag_keys.setdefault(tag, set()).add(key)

    def _drop(self, key: str) -> None:
        """Remove a key and its tag membership from every structure."""
        self._data.pop(key, None)
        tags = self._key_tags.pop(key, None)
        if tags is not None:
            self._remove_from_tags(key, tags)

    def _remove_from_tags(self, key: str, tags: Iterable[str]) -> None:
        """Drop a key from each given tag's forward set, pruning empties."""
        for tag in tags:
            members = self._tag_keys.get(tag)
            if members is None:  # pragma: no cover - forward/reverse in sync
                continue
            members.discard(key)
            if not members:
                del self._tag_keys[tag]
