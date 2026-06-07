"""Stampede protection core shared by `@cached` and `TTLCache.get_or_set`.

A cache stampede (or "dog-pile") happens when many callers miss the
same key at once and all recompute it together. The helpers here fold
those misses into one execution: an in-process per-key lock first, then
a cross-replica `Lock` when the active app exposes a lock backend. The
value is double-checked inside each lock so a caller that arrives after
the work is done returns the fresh value instead of recomputing.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from grelmicro._app import Grelmicro
from grelmicro.coordination.lock import Lock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from grelmicro.cache.ttl import TTLCache

_SENTINEL = object()

_PER_KEY_LOCK_BUDGET = 1024


def _has_lock_backend() -> bool:
    """Return whether the active app exposes a default lock backend.

    Drives ``lock=True`` auto-selection: a cold miss folds across replicas
    when a backend is present and folds in-process otherwise.
    """
    try:
        coordination = Grelmicro.current().get("coordination")
    except LookupError:
        return False
    return coordination._lock_backend is not None  # noqa: SLF001


def _stampede_lock_name(key: str) -> str:
    """Build a backend-safe distributed lock name from a cache key.

    Cache keys embed a function qualname that may contain characters
    (``<locals>``, spaces) that the `Lock` name validator rejects, so we
    hash the key into a fixed, always-valid name.
    """
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"cache.stampede.{digest}"


def _evict_idle_locks(locks: OrderedDict[str, asyncio.Lock]) -> None:
    """Drop the oldest unlocked entries while over the per-key budget.

    Caller must hold the per-owner guard lock. A held lock is kept so a
    concurrent computation cannot lose its mutual-exclusion barrier even
    if the dict has grown past the budget.
    """
    while len(locks) > _PER_KEY_LOCK_BUDGET:
        for stale_key, stale_lock in locks.items():
            if not stale_lock.locked():
                del locks[stale_key]
                break
        else:  # pragma: no cover - every entry currently held
            return


class AsyncStampedeGuard:
    """Per-owner registry of in-process per-key locks.

    Each decorated function and each ``TTLCache`` keeps its own guard so
    keys never collide across owners. Idle locks are evicted once the
    registry grows past a fixed budget.
    """

    def __init__(self) -> None:
        """Initialize an empty lock registry."""
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._guard = asyncio.Lock()

    async def get_lock(self, key: str) -> asyncio.Lock:
        """Return the lock for a key, creating it on first use."""
        async with self._guard:
            the_lock = self._locks.get(key)
            if the_lock is None:
                the_lock = asyncio.Lock()
                self._locks[key] = the_lock
                _evict_idle_locks(self._locks)
            else:
                self._locks.move_to_end(key)
            return the_lock


async def compute_with_stampede(
    cache: TTLCache,
    key: str,
    compute: Callable[[], Awaitable[Any]],
    guard: AsyncStampedeGuard,
    *,
    per_key: bool,
    auto_distributed: bool,
) -> Any:  # noqa: ANN401
    """Run ``compute`` once for ``key`` under stampede protection.

    ``compute`` performs the work and stores the result, returning the
    computed value. ``per_key`` enables the in-process lock, and
    ``auto_distributed`` additionally folds across replicas through a
    cross-replica `Lock` when a lock backend is present. The value is
    double-checked under each lock with `_peek` so a caller that arrives
    after the work is done returns the fresh value.
    """
    if not per_key:
        return await compute()

    the_lock = await guard.get_lock(key)
    async with the_lock:
        result = await cache._peek(key, _SENTINEL)  # noqa: SLF001
        if result is not _SENTINEL:
            return result
        if auto_distributed and _has_lock_backend():
            async with Lock(_stampede_lock_name(key)):
                result = await cache._peek(key, _SENTINEL)  # noqa: SLF001
                if result is not _SENTINEL:
                    return result
                return await compute()
        return await compute()
