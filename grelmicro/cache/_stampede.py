"""Stampede protection core shared by `@cached` and `TTLCache.get_or_set`.

A cache stampede (or "dog-pile") happens when many callers miss the
same key at once and all recompute it together. The helpers here fold
those misses into one execution: an in-process per-key lock first, then,
when the fold reaches past the process, the `Lock` of the app's
`Coordination`. The value is double-checked inside each lock so a caller
that arrives after the work is done returns the fresh value instead of
recomputing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast, get_args

from grelmicro._app import resolve_ambient
from grelmicro._environment import Binding, falls_short, label, record
from grelmicro.coordination.errors import (
    LockAcquireError,
    LockNotOwnedError,
    LockReleaseError,
)
from grelmicro.coordination.lock import Lock
from grelmicro.errors import SettingsValidationError
from grelmicro.types import BackendScope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from grelmicro.cache.ttl import TTLCache

logger = logging.getLogger(__name__)

_SENTINEL = object()

_PER_KEY_LOCK_BUDGET = 1024

_SCOPES: frozenset[object] = frozenset(get_args(BackendScope.__value__))


def _has_lock_backend() -> bool:
    """Return whether the active app has a `Coordination` with a lock backend."""
    try:
        coordination = resolve_ambient(("coordination", "default"))
    except LookupError:
        return False
    return coordination._lock_backend is not None  # noqa: SLF001


def check_fold(lock: object) -> BackendScope | None:
    """Return `lock` when it names how far misses fold, `None` included.

    Raises:
        SettingsValidationError: If `lock` is not a backend scope or `None`.
    """
    if lock is None or (isinstance(lock, str) and lock in _SCOPES):
        return cast("BackendScope | None", lock)
    msg = (
        "lock= takes 'process', 'host', 'cluster' or None. "
        "'process' folds concurrent misses in the process, 'host' and "
        "'cluster' also fold through the app's lock backend, and None "
        "turns folding off."
    )
    raise SettingsValidationError(msg)


class Fold:
    """How far concurrent misses on one key share a single computation.

    `"process"` folds them in the process only. `"host"` and `"cluster"`
    also fold them through the lock backend of the app's `Coordination`,
    which has to reach that far.
    """

    __slots__ = ("__weakref__", "_checked", "_failing", "name", "scope")

    def __init__(
        self, name: str, scope: BackendScope, *, check: bool = True
    ) -> None:
        """Fold the misses of `name` as far as `scope`.

        With `check=False` the lock backend is never compared with the
        scope.
        """
        self.name = name
        self.scope = scope
        self._checked = not check
        self._failing = False

    @property
    def crosses(self) -> bool:
        """Whether misses also fold through the app's lock backend."""
        return self.scope != "process"

    @asynccontextmanager
    async def across(self, key: str) -> AsyncIterator[bool]:
        """Hold `key` across processes for the body, when the scope asks it.

        Yields whether the body runs under the cross-process lock. A lock
        backend that fails to grant the lock is logged once until it
        grants one again, and the body runs folded in the process only. A
        lock lost before its release is logged, and the body's result
        stands.

        Raises:
            OutOfContextError: If the scope reaches past the process and
                the app has no lock backend.
            BackendScopeError: If the app runs in `staging` or
                `production` and its lock backend reaches less far than
                the scope.
        """
        if self.scope == "process":
            yield False
            return
        lock = Lock(_stampede_lock_name(key))
        self._check(lock.backend)
        try:
            await lock.acquire()
        except LockAcquireError:
            if not self._failing:
                logger.warning(
                    "The lock backend failed, so %s folds misses in this "
                    "process only until it answers again.",
                    label(self),
                    exc_info=True,
                )
            self._failing = True
            acquired = False
        else:
            self._failing = False
            acquired = True
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            try:
                await lock.release()
            except LockNotOwnedError, LockReleaseError:
                logger.warning(
                    "%s lost its lock before releasing it, so another "
                    "process may have computed the same key.",
                    label(self),
                    exc_info=True,
                )

    def _check(self, backend: object) -> None:
        """Report a lock backend that reaches less far than the scope, once.

        Raises:
            BackendScopeError: As `record` does.
        """
        if self._checked:
            return
        if falls_short(backend, self.scope) is not None:
            record(
                self,
                Binding(
                    label(self),
                    self.scope,
                    backend=backend,
                    kind="coordination",
                    keyword="lock",
                ),
            )
        self._checked = True


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
    fold: Fold | None,
) -> Any:  # noqa: ANN401
    """Run ``compute`` once for ``key`` under stampede protection.

    ``compute`` performs the work and stores the result, returning the
    computed value. ``fold`` says how far concurrent misses share it, and
    ``None`` runs ``compute`` for every caller. The value is
    double-checked under each lock with `_peek` so a caller that arrives
    after the work is done returns the fresh value.
    """
    if fold is None:
        return await compute()

    the_lock = await guard.get_lock(key)
    async with the_lock:
        result = await cache._peek(key, _SENTINEL)  # noqa: SLF001
        if result is not _SENTINEL:
            return result
        async with fold.across(key) as crossed:
            if crossed:
                result = await cache._peek(key, _SENTINEL)  # noqa: SLF001
                if result is not _SENTINEL:
                    return result
            return await compute()


async def stream_with_stampede(
    cache: TTLCache,
    key: str,
    produce: Callable[[], AsyncIterator[Any]],
    guard: AsyncStampedeGuard,
    *,
    fold: Fold | None,
) -> AsyncIterator[Any]:
    """Stream ``produce`` once for ``key`` under stampede protection.

    The streaming twin of `compute_with_stampede`, holding the same locks
    for the whole stream. A second caller therefore waits, then replays
    the stored entry from the double-check rather than producing it a
    second time. A caller that stops reading closes ``produce`` without
    storing anything, and releases the locks on its way out.
    """
    if fold is None:
        async for item in produce():
            yield item
        return

    the_lock = await guard.get_lock(key)
    async with the_lock:
        result: Any = await cache._peek(key, _SENTINEL)  # noqa: SLF001
        if result is not _SENTINEL:
            for item in result:
                yield item
            return
        async with fold.across(key) as crossed:
            if crossed:
                shared: Any = await cache._peek(key, _SENTINEL)  # noqa: SLF001
                if shared is not _SENTINEL:
                    for item in shared:
                        yield item
                    return
            async for item in produce():
                yield item
