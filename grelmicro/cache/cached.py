"""Cached Decorator."""

import asyncio
import functools
import threading
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Annotated, Any, ParamSpec, TypeVar

from typing_extensions import Doc

from grelmicro import _from_thread
from grelmicro.cache._key import make_cache_key
from grelmicro.cache.ttl import TTLCache

# Decorator factories cannot use PEP 695 cleanly: the inner
# ``decorator`` would inherit ``cached``'s type parameters instead
# of being fresh-generic per decoration site. Module-level
# ``ParamSpec``/``TypeVar`` is the working pattern.
P = ParamSpec("P")
R = TypeVar("R")

_SENTINEL = object()

# Lock parameter: True auto-creates, or pass your own.
_LockType = (
    bool | AbstractContextManager[Any] | AbstractAsyncContextManager[Any] | None
)


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
        _LockType,
        Doc(
            """
            Protect against duplicate work on a cache miss. When
            the cache does not have the value, only one caller
            runs the function. The other callers wait for the
            result.

            Set to ``True`` for **per-key** locking. Misses on
            different keys run in parallel. Misses on the same
            key run one at a time. The right lock type is
            created automatically (``asyncio.Lock`` for async,
            ``threading.Lock`` for sync).

            You can also pass a custom context manager for
            **global** locking. This uses a single lock shared
            by all keys.
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

    Returns:
        A decorator that caches function results.
    """

    def decorator(
        func: Callable[P, R],
    ) -> Callable[P, R]:
        is_async_func = asyncio.iscoroutinefunction(func)
        per_key = lock is True
        global_lock = _resolve_global_lock(lock)

        if is_async_func:
            wrapper = _build_async_wrapper(
                func,
                cache,
                key_maker,
                skip,
                typed=typed,
                global_lock=global_lock,
                per_key=per_key,
            )
        else:
            wrapper = _build_sync_wrapper(
                func,
                cache,
                key_maker,
                skip,
                typed=typed,
                global_lock=global_lock,
                per_key=per_key,
            )
        wrapper.cache_info = cache.cache_info
        wrapper.cache_clear = cache.clear
        return wrapper

    return decorator


def _resolve_global_lock(
    lock: _LockType,
) -> AbstractContextManager[Any] | AbstractAsyncContextManager[Any] | None:
    """Resolve the lock parameter to a global lock instance.

    Returns None for ``True`` (per-key locks are handled in wrappers),
    ``False``, and ``None``. Returns the custom instance as-is.
    """
    if lock is True or lock is False or lock is None:
        return None
    return lock


# --- Async function ---


def _build_async_wrapper(
    func: Any,  # noqa: ANN401
    cache: TTLCache,
    key_maker: Any,  # noqa: ANN401
    skip: Any,  # noqa: ANN401
    *,
    typed: bool,
    global_lock: Any,  # noqa: ANN401
    per_key: bool,
) -> Any:  # noqa: ANN401
    """Build async wrapper for cached decorator."""
    key_locks: dict[str, asyncio.Lock] = {}
    key_locks_guard = asyncio.Lock()

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        key = _make_key(func, args, kwargs, key_maker, typed=typed)
        result = await cache.get(key, _SENTINEL)
        if result is not _SENTINEL:
            return result

        if per_key:
            async with key_locks_guard:
                the_lock = key_locks.setdefault(key, asyncio.Lock())
        else:
            the_lock = global_lock
        if the_lock is not None:
            async with the_lock:
                result = await cache.get(key, _SENTINEL)
                if result is not _SENTINEL:
                    return result
                return await _compute_and_cache(
                    func,
                    args,
                    kwargs,
                    cache,
                    key,
                    skip,
                )
        return await _compute_and_cache(
            func,
            args,
            kwargs,
            cache,
            key,
            skip,
        )

    return async_wrapper


async def _compute_and_cache(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: TTLCache,
    key: str,
    skip: Callable[[Any], bool] | None,
) -> Any:  # noqa: ANN401
    """Execute async function and store result in cache."""
    result = await func(*args, **kwargs)
    if skip is None or not skip(result):
        await cache.set(key, result)
    return result


# --- Sync function (delegates to async cache via from_thread) ---


def _build_sync_wrapper(
    func: Any,  # noqa: ANN401
    cache: TTLCache,
    key_maker: Any,  # noqa: ANN401
    skip: Any,  # noqa: ANN401
    *,
    typed: bool,
    global_lock: Any,  # noqa: ANN401
    per_key: bool,
) -> Any:  # noqa: ANN401
    """Build sync wrapper for cached decorator.

    Sync functions call the async cache via
    ``grelmicro._from_thread.run``, which requires the parent event
    loop to be reachable (spawn the worker via
    ``grelmicro.to_thread.run_sync`` or call any grelmicro async API on
    the cache from the parent task first).
    """
    key_locks: dict[str, threading.Lock] = {}
    key_locks_guard = threading.Lock()

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        key = _make_key(func, args, kwargs, key_maker, typed=typed)
        result = _from_thread.run(cache._loop, cache.get, key, _SENTINEL)  # noqa: SLF001
        if result is not _SENTINEL:
            return result

        if per_key:
            with key_locks_guard:
                the_lock = key_locks.setdefault(key, threading.Lock())
        else:
            the_lock = global_lock

        if the_lock is not None:
            with the_lock:
                result = _from_thread.run(
                    cache._loop,  # noqa: SLF001
                    cache.get,
                    key,
                    _SENTINEL,
                )
                if result is not _SENTINEL:
                    return result
                return _compute_and_cache_sync(
                    func,
                    args,
                    kwargs,
                    cache,
                    key,
                    skip,
                )
        return _compute_and_cache_sync(
            func,
            args,
            kwargs,
            cache,
            key,
            skip,
        )

    return sync_wrapper


def _compute_and_cache_sync(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: TTLCache,
    key: str,
    skip: Callable[[Any], bool] | None,
) -> Any:  # noqa: ANN401
    """Execute sync function and store result in async cache."""
    result = func(*args, **kwargs)
    if skip is None or not skip(result):
        _from_thread.run(cache._loop, cache.set, key, result)  # noqa: SLF001
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
