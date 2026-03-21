"""TTL Cache."""

from dataclasses import dataclass
from time import monotonic
from typing import Annotated, Any

from typing_extensions import Doc


@dataclass(frozen=True, slots=True)
class CacheInfo:
    """Cache statistics snapshot.

    Attributes:
        hits: Number of cache hits.
        misses: Number of cache misses.
        maxsize: Maximum number of entries (``0`` means unlimited).
        currsize: Current number of stored entries (may include expired).
        evictions: Number of entries evicted to make room.
    """

    hits: int
    misses: int
    maxsize: int
    currsize: int
    evictions: int


class TTLCache:
    """Synchronous in-memory cache with per-entry TTL and LRU eviction.

    A dict-like cache where each entry expires after a configurable
    time-to-live (TTL). Expiry is checked lazily on access. When the
    cache is full, the oldest expired entry is evicted first, then the
    least recently used entry (LRU).

    Accessing or overwriting a key promotes it to most-recently-used.

    Not thread-safe — the caller is responsible for synchronization.

    Raises:
        ValueError: If maxsize is negative or ttl is not positive.
    """

    def __init__(
        self,
        maxsize: Annotated[
            int,
            Doc(
                """
                Maximum number of entries. ``0`` means unlimited.
                """,
            ),
        ],
        ttl: Annotated[
            float,
            Doc(
                """
                Default TTL in seconds for all entries.
                """,
            ),
        ],
    ) -> None:
        """Initialize the cache."""
        if maxsize < 0:
            msg = "maxsize must be non-negative"
            raise ValueError(msg)
        if ttl <= 0:
            msg = "ttl must be positive"
            raise ValueError(msg)
        self._maxsize = maxsize
        self._ttl = ttl
        # Stores: key -> (value, expiry_time)
        self._data: dict[str, tuple[Any, float]] = {}
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(
        self,
        key: Annotated[
            str,
            Doc(
                """
                The cache key.
                """,
            ),
        ],
        default: Annotated[  # noqa: ANN401
            Any,
            Doc(
                """
                Value to return if key is not found.
                """,
            ),
        ] = None,
    ) -> Any:  # noqa: ANN401
        """Get a value by key.

        Returns the default if the key is missing or expired.
        A successful hit promotes the key to most-recently-used.

        Returns:
            The cached value or the default.
        """
        entry = self._data.get(key)
        if entry is None:
            self._misses += 1
            return default
        value, expiry = entry
        if monotonic() >= expiry:
            del self._data[key]
            self._misses += 1
            return default
        # Move to end (most recently used) via delete + reinsert
        del self._data[key]
        self._data[key] = entry
        self._hits += 1
        return value

    def set(
        self,
        key: Annotated[
            str,
            Doc(
                """
                The cache key.
                """,
            ),
        ],
        value: Annotated[  # noqa: ANN401
            Any,
            Doc(
                """
                The value to cache.
                """,
            ),
        ],
        ttl: Annotated[
            float | None,
            Doc(
                """
                Optional TTL override in seconds.
                """,
            ),
        ] = None,
    ) -> None:
        """Set a value with an optional per-entry TTL override.

        If the cache is full, the oldest expired entry is evicted
        first. If no expired entries exist, the least recently used
        entry is evicted.

        Raises:
            ValueError: If ttl is not positive.
        """
        if ttl is not None and ttl <= 0:
            msg = "ttl must be positive"
            raise ValueError(msg)
        # If key already exists, remove it so reinsertion goes to end
        if key in self._data:
            del self._data[key]
        elif self._maxsize > 0 and len(self._data) >= self._maxsize:
            self._evict()
        entry_ttl = ttl if ttl is not None else self._ttl
        self._data[key] = (value, monotonic() + entry_ttl)

    def delete(
        self,
        key: Annotated[
            str,
            Doc(
                """
                The cache key to delete.
                """,
            ),
        ],
    ) -> None:
        """Delete a key from the cache.

        No-op if the key does not exist.
        """
        self._data.pop(key, None)

    def clear(self) -> None:
        """Remove all entries from the cache."""
        self._data.clear()

    def cache_info(self) -> CacheInfo:
        """Return a snapshot of cache statistics."""
        return CacheInfo(
            hits=self._hits,
            misses=self._misses,
            maxsize=self._maxsize,
            currsize=len(self._data),
            evictions=self._evictions,
        )

    def __contains__(self, key: str) -> bool:
        """Check if a key exists and is not expired.

        Does not promote the key in LRU order or update
        hit/miss statistics. Use ``get()`` for that.
        """
        entry = self._data.get(key)
        if entry is None:
            return False
        _, expiry = entry
        if monotonic() >= expiry:
            del self._data[key]
            return False
        return True

    def __len__(self) -> int:
        """Return the number of entries, including expired ones.

        Expired entries are not purged eagerly to avoid a full scan.
        """
        return len(self._data)

    def _evict(self) -> None:
        """Evict one entry to make room for a new one.

        Strategy: remove the oldest expired entry first. If none are
        expired, remove the least recently used entry (LRU — first
        in insertion order). Scans entries in order — O(n) in the
        worst case.
        """
        now = monotonic()
        oldest_expired_key: str | None = None
        for k, (_, expiry) in self._data.items():
            if now >= expiry:
                oldest_expired_key = k
                break
        if oldest_expired_key is not None:
            del self._data[oldest_expired_key]
        else:
            # Remove the first (LRU) entry
            first_key = next(iter(self._data))
            del self._data[first_key]
        self._evictions += 1
