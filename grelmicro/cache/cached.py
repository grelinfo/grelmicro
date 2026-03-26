"""Cached Decorator."""

import asyncio
import functools
import threading
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Annotated, Any, ParamSpec, TypeVar

from typing_extensions import Doc

from grelmicro.cache._key import make_cache_key
from grelmicro.cache._protocol import Cache

P = ParamSpec("P")
R = TypeVar("R")

_SENTINEL = object()

# Lock parameter: True auto-creates, or pass your own.
_LockType = (
    bool | AbstractContextManager[Any] | AbstractAsyncContextManager[Any] | None
)


def cached(
    cache: Annotated[
        Cache,
        Doc(
            """
            The cache instance to store results in.
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
    serializer: Annotated[
        Callable[[Any], bytes] | None,
        Doc(
            """
            Optional serializer for cached values. When provided,
            values are serialized before storing.
            """,
        ),
    ] = None,
    deserializer: Annotated[
        Callable[[bytes], Any] | None,
        Doc(
            """
            Optional deserializer for cached values. When provided,
            values are deserialized after retrieval.
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
            Enable stampede protection. When a cache miss occurs,
            only one caller executes the function while all others
            block until the result is available.

            Set to ``True`` to enable with **per-key** locking:
            concurrent misses on different keys proceed in parallel,
            only callers hitting the same key are serialized.
            The appropriate lock type is auto-created
            (``asyncio.Lock`` for async, ``threading.Lock`` for
            sync).

            You can also pass a custom context manager instance
            for **global** locking (a single lock shared across
            all keys).
            """,
        ),
    ] = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Cache decorator for sync and async functions.

    Automatically detects whether the decorated function is sync or
    async and wraps it accordingly. Cached values are stored in the
    provided ``Cache`` instance.

    The decorated function exposes ``cache_info()`` and
    ``cache_clear()`` helpers (matching ``functools.lru_cache``).

    Returns:
        A decorator that caches function results.

    Raises:
        ValueError: If only one of serializer/deserializer is given.
    """
    if (serializer is None) != (deserializer is None):
        msg = "serializer and deserializer must be provided together"
        raise ValueError(msg)

    def decorator(
        func: Callable[P, R],
    ) -> Callable[P, R]:
        is_async = asyncio.iscoroutinefunction(func)
        per_key = lock is True
        global_lock = _resolve_global_lock(lock)

        if is_async:
            wrapper = _build_async_wrapper(
                func,
                cache,
                key_maker,
                serializer,
                deserializer,
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
                serializer,
                deserializer,
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


def _build_async_wrapper(
    func: Any,  # noqa: ANN401
    cache: Cache,
    key_maker: Any,  # noqa: ANN401
    serializer: Any,  # noqa: ANN401
    deserializer: Any,  # noqa: ANN401
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
        result = cache.get(key, _SENTINEL)
        if result is not _SENTINEL:
            return _deserialize(result, deserializer)

        if per_key:
            async with key_locks_guard:
                lock = key_locks.setdefault(key, asyncio.Lock())
        else:
            lock = global_lock
        if lock is not None:
            async with lock:
                result = cache.get(key, _SENTINEL)
                if result is not _SENTINEL:
                    return _deserialize(result, deserializer)
                return await _compute_and_cache_async(
                    func,
                    args,
                    kwargs,
                    cache,
                    key,
                    serializer,
                    skip,
                )
        return await _compute_and_cache_async(
            func,
            args,
            kwargs,
            cache,
            key,
            serializer,
            skip,
        )

    return async_wrapper


def _build_sync_wrapper(
    func: Any,  # noqa: ANN401
    cache: Cache,
    key_maker: Any,  # noqa: ANN401
    serializer: Any,  # noqa: ANN401
    deserializer: Any,  # noqa: ANN401
    skip: Any,  # noqa: ANN401
    *,
    typed: bool,
    global_lock: Any,  # noqa: ANN401
    per_key: bool,
) -> Any:  # noqa: ANN401
    """Build sync wrapper for cached decorator."""
    key_locks: dict[str, threading.Lock] = {}
    key_locks_guard = threading.Lock()

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        key = _make_key(func, args, kwargs, key_maker, typed=typed)
        result = cache.get(key, _SENTINEL)
        if result is not _SENTINEL:
            return _deserialize(result, deserializer)

        if per_key:
            with key_locks_guard:
                lock = key_locks.setdefault(key, threading.Lock())
        else:
            lock = global_lock

        if lock is not None:
            with lock:
                result = cache.get(key, _SENTINEL)
                if result is not _SENTINEL:
                    return _deserialize(result, deserializer)
                return _compute_and_cache(
                    func,
                    args,
                    kwargs,
                    cache,
                    key,
                    serializer,
                    skip,
                )
        return _compute_and_cache(
            func,
            args,
            kwargs,
            cache,
            key,
            serializer,
            skip,
        )

    return sync_wrapper


async def _compute_and_cache_async(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: Cache,
    key: str,
    serializer: Callable[[Any], bytes] | None,
    skip: Callable[[Any], bool] | None,
) -> Any:  # noqa: ANN401
    """Execute async function and store result in cache."""
    result = await func(*args, **kwargs)
    if skip is None or not skip(result):
        cache.set(key, _serialize(result, serializer))
    return result


def _compute_and_cache(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    cache: Cache,
    key: str,
    serializer: Callable[[Any], bytes] | None,
    skip: Callable[[Any], bool] | None,
) -> Any:  # noqa: ANN401
    """Execute sync function and store result in cache."""
    result = func(*args, **kwargs)
    if skip is None or not skip(result):
        cache.set(key, _serialize(result, serializer))
    return result


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


def _serialize(
    value: Any,  # noqa: ANN401
    serializer: Callable[[Any], bytes] | None,
) -> Any:  # noqa: ANN401
    if serializer is not None:
        return serializer(value)
    return value


def _deserialize(
    value: Any,  # noqa: ANN401
    deserializer: Callable[[bytes], Any] | None,
) -> Any:  # noqa: ANN401
    if deserializer is not None:
        return deserializer(value)
    return value
