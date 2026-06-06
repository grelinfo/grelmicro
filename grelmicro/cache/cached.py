"""Cached Decorator."""

import asyncio
import functools
import hashlib
import json
import math
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Annotated, Any, Literal, ParamSpec, TypeVar

from typing_extensions import Doc

from grelmicro._app import Grelmicro
from grelmicro.cache._key import make_cache_key
from grelmicro.cache.ttl import _CACHE_PREFIX, TTLCache
from grelmicro.sync.lock import Lock

# Decorator factories cannot use PEP 695 cleanly: the inner
# ``decorator`` would inherit ``cached``'s type parameters instead
# of being fresh-generic per decoration site. Module-level
# ``ParamSpec``/``TypeVar`` is the working pattern.
P = ParamSpec("P")
R = TypeVar("R")

_SENTINEL = object()

_PER_KEY_LOCK_BUDGET = 1024

_XFETCH_SUFFIX = "\x00xf"

# Seams rebound by tests for deterministic ``early`` behavior. ``_random``
# rolls the XFetch die; ``_now`` is the wall clock that ages entries.
_random = random.random
_now = time.time


def _evict_idle_locks(
    locks: OrderedDict[str, asyncio.Lock],
) -> None:
    """Drop the oldest unlocked entries while over the per-key budget.

    Caller must hold the per-decorator guard lock. A held lock is kept
    so a concurrent computation cannot lose its mutual-exclusion barrier
    even if the dict has grown past the budget.
    """
    while len(locks) > _PER_KEY_LOCK_BUDGET:
        for stale_key, stale_lock in locks.items():
            if not stale_lock.locked():
                del locks[stale_key]
                break
        else:  # pragma: no cover - every entry currently held
            return


def _evict_idle_locks_sync(
    locks: OrderedDict[str, threading.Lock],
) -> None:
    """Drop the oldest unheld entries while over the per-key budget.

    Caller must hold the per-decorator guard lock. ``threading.Lock``
    has no public ``locked()`` accessor, so we probe with a non-
    blocking ``acquire``: success means the lock was idle, and we
    release immediately so behavior is unchanged.
    """
    while len(locks) > _PER_KEY_LOCK_BUDGET:
        for stale_key, stale_lock in locks.items():
            if stale_lock.acquire(blocking=False):
                stale_lock.release()
                del locks[stale_key]
                break
        else:  # pragma: no cover - every entry currently held
            return


def cached(
    cache: Annotated[
        TTLCache,
        Doc(
            """
            The TTLCache instance to store results in.
            """,
        ),
    ],
    *,
    key_maker: Annotated[
        Callable[[Callable[..., Any], tuple[Any, ...], dict[str, Any]], str]
        | None,
        Doc(
            """
            Optional custom key generation function. Receives
            ``(func, args, kwargs)`` and must return a string key.
            """,
        ),
    ] = None,
    skip: Annotated[
        Callable[[Any], bool] | None,
        Doc(
            """
            Optional predicate receiving the function result. When
            it returns ``True`` the result is **not** cached.
            """,
        ),
    ] = None,
    typed: Annotated[
        bool,
        Doc(
            """
            If ``True``, arguments of different types are cached
            separately (e.g. ``3`` vs ``3.0``).
            """,
        ),
    ] = False,
    lock: Annotated[
        bool | Literal["local"],
        Doc(
            """
            Protect against duplicate work when many callers miss the
            same key at once (the "dog-pile" effect).

            - ``False`` (default): no protection. Every concurrent miss
              runs the function.
            - ``True``: fold concurrent misses to one execution. When the
              active `Grelmicro` app has a `Sync` backend, misses fold
              across replicas through it. Otherwise an in-process lock
              folds them within the worker. An in-process lock is always
              applied first, so the backend is hit once per cold miss.
            - ``"local"``: force the in-process lock only, even when a
              `Sync` backend is configured. Use when per-replica recompute
              is acceptable and you want no backend round-trip on a cold
              miss.
            """,
        ),
    ] = False,
    early: Annotated[
        float | None,
        Doc(
            """
            Probabilistic early refresh (XFetch). A float in ``[0, 1)``.
            When a cached entry is read inside the last ``early``
            fraction of its TTL, the call may roll a die and, on success,
            schedule a background recompute while still returning the
            cached value. The hottest keys then refresh before they
            expire, so no caller ever blocks on a cold miss.

            Costs one extra recompute per refresh. Leave ``None`` to
            disable.
            """,
        ),
    ] = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Cache decorator for sync and async functions.

    Automatically detects whether the decorated function is sync or
    async and wraps it accordingly.

    The decorated function exposes ``cache_info()`` and
    ``cache_clear()`` helpers (matching ``functools.lru_cache``).
    ``cache_clear()`` is always a coroutine (must be awaited).

    Raises:
        ValueError: If ``lock`` is not ``True``, ``False``, or ``"local"``,
            or if ``early`` is outside ``[0, 1)``.

    Returns:
        A decorator that caches function results.
    """
    if lock not in (True, False, "local"):
        msg = f"Invalid lock {lock!r}: use True, False, or 'local'."
        raise ValueError(msg)
    if early is not None and not 0 <= early < 1:
        msg = f"Invalid early {early!r}: must be a float in [0, 1)."
        raise ValueError(msg)

    def decorator(
        func: Callable[P, R],
    ) -> Callable[P, R]:
        is_async_func = asyncio.iscoroutinefunction(func)
        per_key = lock is not False
        auto_distributed = lock is True

        if is_async_func:
            wrapper = _build_async_wrapper(
                func,
                cache,
                key_maker,
                skip,
                typed=typed,
                per_key=per_key,
                auto_distributed=auto_distributed,
                early=early,
            )
        else:
            wrapper = _build_sync_wrapper(
                func,
                cache,
                key_maker,
                skip,
                typed=typed,
                per_key=per_key,
                auto_distributed=auto_distributed,
                early=early,
            )
        wrapper.cache_info = cache.cache_info
        wrapper.cache_clear = cache.clear
        return wrapper

    return decorator


# --- Stampede helpers ---


def _has_sync_backend() -> bool:
    """Return whether the active app exposes a default `Sync` backend.

    Drives ``lock=True`` auto-selection: a cold miss folds across replicas
    when a backend is present and folds in-process otherwise.
    """
    try:
        Grelmicro.current().get("sync")
    except LookupError:
        return False
    return True


def _stampede_lock_name(key: str) -> str:
    """Build a backend-safe distributed lock name from a cache key.

    Cache keys embed a function qualname that may contain characters
    (``<locals>``, spaces) that the `Lock` name validator rejects, so we
    hash the key into a fixed, always-valid name.
    """
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"cache.stampede.{digest}"


def _xfetch_should_refresh(remaining: float, delta: float) -> bool:
    """Roll the Vattani XFetch die for an entry in its early window.

    ``remaining`` is the seconds left before expiry and ``delta`` is the
    last recompute duration. The entry refreshes when
    ``delta * -ln(rand) >= remaining``, so a key whose recompute is
    expensive relative to its remaining life refreshes sooner.
    """
    return delta * -math.log(_random()) >= remaining


async def _read_meta(cache: TTLCache, key: str) -> tuple[float, float] | None:
    """Return ``(written_epoch, delta)`` for an XFetch entry, or None."""
    raw = await cache._get_backend().get(  # noqa: SLF001
        key=f"{_CACHE_PREFIX}:{key}{_XFETCH_SUFFIX}"
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return float(data["w"]), float(data["d"])
    except (ValueError, KeyError, TypeError):  # pragma: no cover - corrupt
        return None


async def _write_meta(
    cache: TTLCache, key: str, written: float, delta: float
) -> None:
    """Store the XFetch ``(written, delta)`` sidecar next to a value."""
    raw = json.dumps({"w": written, "d": delta}).encode()
    await cache._get_backend().set(  # noqa: SLF001
        key=f"{_CACHE_PREFIX}:{key}{_XFETCH_SUFFIX}",
        value=raw,
        ttl=cache.config.ttl,
    )


def _due_for_early_refresh(
    meta: tuple[float, float] | None, ttl: float, early: float
) -> bool:
    """Decide whether a read should trigger a background refresh."""
    if meta is None:
        return False
    written, delta = meta
    remaining = written + ttl - _now()
    if remaining <= 0 or remaining > early * ttl:
        # Outside the early window (or already expired and refetched).
        return False
    return _xfetch_should_refresh(remaining, delta)


# --- Async function ---


def _build_async_wrapper(
    func: Any,  # noqa: ANN401
    cache: TTLCache,
    key_maker: Any,  # noqa: ANN401
    skip: Any,  # noqa: ANN401
    *,
    typed: bool,
    per_key: bool,
    auto_distributed: bool,
    early: float | None,
) -> Any:  # noqa: ANN401
    """Build async wrapper for cached decorator."""
    key_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
    key_locks_guard = asyncio.Lock()

    async def get_key_lock(key: str) -> asyncio.Lock:
        async with key_locks_guard:
            the_lock = key_locks.get(key)
            if the_lock is None:
                the_lock = asyncio.Lock()
                key_locks[key] = the_lock
                _evict_idle_locks(key_locks)
            else:
                key_locks.move_to_end(key)
            return the_lock

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        key = _make_key(func, args, kwargs, key_maker, typed=typed)
        result = await cache.get(key, _SENTINEL)
        if result is not _SENTINEL:
            if early is not None:
                await _maybe_refresh_async(
                    func, args, kwargs, cache, key, skip, early, get_key_lock
                )
            return result

        if not per_key:
            return await _compute_and_cache(
                func, args, kwargs, cache, key, skip, early=early
            )

        the_lock = await get_key_lock(key)
        async with the_lock:
            result = await cache._peek(key, _SENTINEL)  # noqa: SLF001
            if result is not _SENTINEL:
                return result
            if auto_distributed and _has_sync_backend():
                return await _compute_distributed(
                    func, args, kwargs, cache, key, skip, early=early
                )
            return await _compute_and_cache(
                func, args, kwargs, cache, key, skip, early=early
            )

    return async_wrapper


async def _compute_distributed(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: TTLCache,
    key: str,
    skip: Callable[[Any], bool] | None,
    *,
    early: float | None,
) -> Any:  # noqa: ANN401
    """Compute under a cross-replica `Sync` lock, re-checking inside it."""
    async with Lock(_stampede_lock_name(key)):
        result = await cache._peek(key, _SENTINEL)  # noqa: SLF001
        if result is not _SENTINEL:
            return result
        return await _compute_and_cache(
            func, args, kwargs, cache, key, skip, early=early
        )


async def _maybe_refresh_async(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: TTLCache,
    key: str,
    skip: Callable[[Any], bool] | None,
    early: float,
    get_key_lock: Callable[[str], Any],
) -> None:
    """Schedule a background recompute when an entry is due for refresh."""
    meta = await _read_meta(cache, key)
    if not _due_for_early_refresh(meta, cache.config.ttl, early):
        return
    the_lock = await get_key_lock(key)
    if the_lock.locked():
        # A refresh or cold miss is already computing this key.
        return

    async def refresh() -> None:
        if the_lock.locked():  # pragma: no cover - raced lock
            return
        async with the_lock:
            await _compute_and_cache(
                func, args, kwargs, cache, key, skip, early=early
            )

    task = asyncio.create_task(refresh())
    # Drop the lookup table reference once done; surfacing failures is
    # the caller's job via the cache, not this best-effort refresh.
    task.add_done_callback(lambda _: None)


async def _compute_and_cache(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: TTLCache,
    key: str,
    skip: Callable[[Any], bool] | None,
    *,
    early: float | None,
) -> Any:  # noqa: ANN401
    """Execute async function and store result in cache."""
    started = time.perf_counter()
    result = await func(*args, **kwargs)
    delta = time.perf_counter() - started
    if skip is None or not skip(result):
        await cache.set(key, result)
        if early is not None:
            await _write_meta(cache, key, _now(), delta)
    return result


# --- Sync function (delegates to async cache via from_thread) ---


def _build_sync_wrapper(
    func: Any,  # noqa: ANN401
    cache: TTLCache,
    key_maker: Any,  # noqa: ANN401
    skip: Any,  # noqa: ANN401
    *,
    typed: bool,
    per_key: bool,
    auto_distributed: bool,
    early: float | None,
) -> Any:  # noqa: ANN401
    """Build sync wrapper for cached decorator.

    The cache must be touched from the running event loop once
    (typically inside lifespan startup) before the sync wrapper can
    reach it from a worker thread.
    """
    key_locks: OrderedDict[str, threading.Lock] = OrderedDict()
    key_locks_guard = threading.Lock()

    def get_key_lock(key: str) -> threading.Lock:
        with key_locks_guard:
            the_lock = key_locks.get(key)
            if the_lock is None:
                the_lock = threading.Lock()
                key_locks[key] = the_lock
                _evict_idle_locks_sync(key_locks)
            else:
                key_locks.move_to_end(key)
            return the_lock

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        key = _make_key(func, args, kwargs, key_maker, typed=typed)
        loop = cache._get_backend()._loop  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        result = _run(cache.get(key, _SENTINEL), loop)
        if result is not _SENTINEL:
            if early is not None:
                _maybe_refresh_sync(
                    func,
                    args,
                    kwargs,
                    cache,
                    key,
                    skip,
                    early,
                    loop,
                    get_key_lock,
                )
            return result

        if not per_key:
            return _compute_and_cache_sync(
                func, args, kwargs, cache, key, skip, loop, early=early
            )

        the_lock = get_key_lock(key)
        with the_lock:
            result = _run(cache._peek(key, _SENTINEL), loop)  # noqa: SLF001
            if result is not _SENTINEL:
                return result
            if auto_distributed and _has_sync_backend():
                return _run(
                    _distributed_orchestrate(
                        func, args, kwargs, cache, key, skip, loop, early=early
                    ),
                    loop,
                )
            return _compute_and_cache_sync(
                func, args, kwargs, cache, key, skip, loop, early=early
            )

    return sync_wrapper


def _run(coro: Any, loop: Any) -> Any:  # noqa: ANN401
    """Run a coroutine on the cache's event loop from a worker thread."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


async def _distributed_orchestrate(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: TTLCache,
    key: str,
    skip: Callable[[Any], bool] | None,
    loop: Any,  # noqa: ANN401
    *,
    early: float | None,
) -> Any:  # noqa: ANN401
    """Hold the cross-replica lock and recompute, all in one loop task.

    The blocking function runs in an executor so it does not stall the
    loop, while the `Lock` acquire and release stay on the same task so
    ownership holds.
    """
    async with Lock(_stampede_lock_name(key)):
        result = await cache._peek(key, _SENTINEL)  # noqa: SLF001
        if result is not _SENTINEL:
            return result
        started = time.perf_counter()
        result = await loop.run_in_executor(None, lambda: func(*args, **kwargs))
        delta = time.perf_counter() - started
        if skip is None or not skip(result):
            await cache.set(key, result)
            if early is not None:
                await _write_meta(cache, key, _now(), delta)
        return result


def _maybe_refresh_sync(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: TTLCache,
    key: str,
    skip: Callable[[Any], bool] | None,
    early: float,
    loop: Any,  # noqa: ANN401
    get_key_lock: Callable[[str], threading.Lock],
) -> None:
    """Schedule a background recompute for a sync entry due for refresh."""
    meta = _run(_read_meta(cache, key), loop)
    if not _due_for_early_refresh(meta, cache.config.ttl, early):
        return
    the_lock = get_key_lock(key)
    if not the_lock.acquire(blocking=False):
        return

    def refresh() -> None:
        try:
            _compute_and_cache_sync(
                func, args, kwargs, cache, key, skip, loop, early=early
            )
        finally:
            the_lock.release()

    threading.Thread(target=refresh, daemon=True).start()


def _compute_and_cache_sync(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: TTLCache,
    key: str,
    skip: Callable[[Any], bool] | None,
    loop: Any,  # noqa: ANN401
    *,
    early: float | None,
) -> Any:  # noqa: ANN401
    """Execute sync function and store result in async cache."""
    started = time.perf_counter()
    result = func(*args, **kwargs)
    delta = time.perf_counter() - started
    if skip is None or not skip(result):
        _run(cache.set(key, result), loop)
        if early is not None:
            _run(_write_meta(cache, key, _now(), delta), loop)
    return result


# --- Shared helpers ---


def _make_key(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    key_maker: Callable[
        [Callable[..., Any], tuple[Any, ...], dict[str, Any]], str
    ]
    | None,
    *,
    typed: bool,
) -> str:
    if key_maker is not None:
        return key_maker(func, args, kwargs)
    return make_cache_key(func, args, kwargs, typed=typed)
