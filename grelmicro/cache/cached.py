"""Cached Decorator."""

import asyncio
import functools
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, ParamSpec, TypeVar

from grelmicro.cache._key import make_cache_key
from grelmicro.cache.ttl import TTLCache

P = ParamSpec("P")
R = TypeVar("R")

_SENTINEL = object()


def cached(
    cache: TTLCache,
    *,
    key_maker: Callable[
        [Callable[..., Any], tuple[Any, ...], dict[str, Any]], str
    ]
    | None = None,
    serializer: Callable[[Any], bytes] | None = None,
    deserializer: Callable[[bytes], Any] | None = None,
    skip: Callable[[Any], bool] | None = None,
    typed: bool = False,
    lock: AbstractContextManager[Any]
    | AbstractAsyncContextManager[Any]
    | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Cache decorator for sync and async functions.

    Automatically detects whether the decorated function is sync or
    async and wraps it accordingly. Cached values are stored in the
    provided ``TTLCache`` instance.

    The decorated function exposes ``cache_info()`` and
    ``cache_clear()`` helpers (matching ``functools.lru_cache``).

    Args:
        cache: The cache instance to store results in.
        key_maker: Optional custom key generation function. Receives
            ``(func, args, kwargs)`` and must return a string key.
        serializer: Optional serializer for cached values. When
            provided, values are serialized before storing.
        deserializer: Optional deserializer for cached values. When
            provided, values are deserialized after retrieval.
        skip: Optional predicate receiving the function result.
            When it returns ``True`` the result is **not** cached.
        typed: If ``True``, arguments of different types are cached
            separately (e.g. ``3`` vs ``3.0``).
        lock: Optional context manager for stampede protection.
            When provided, only one caller recomputes on cache miss
            while others wait. The lock is **global** (not per-key),
            so a miss on one key blocks all other misses until
            resolved. Use ``asyncio.Lock()`` for async functions or
            ``threading.Lock()`` for sync functions.

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
        if asyncio.iscoroutinefunction(func):
            wrapper = _build_async_wrapper(
                func,
                cache,
                key_maker,
                serializer,
                deserializer,
                skip,
                typed=typed,
                lock=lock,
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
                lock=lock,
            )
        wrapper.cache_info = cache.cache_info
        wrapper.cache_clear = cache.clear
        return wrapper

    return decorator


def _build_async_wrapper(
    func: Any,  # noqa: ANN401
    cache: TTLCache,
    key_maker: Any,  # noqa: ANN401
    serializer: Any,  # noqa: ANN401
    deserializer: Any,  # noqa: ANN401
    skip: Any,  # noqa: ANN401
    *,
    typed: bool,
    lock: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Build async wrapper for cached decorator."""

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        key = _make_key(func, args, kwargs, key_maker, typed=typed)
        result = cache.get(key, _SENTINEL)
        if result is not _SENTINEL:
            return _deserialize(result, deserializer)
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
    cache: TTLCache,
    key_maker: Any,  # noqa: ANN401
    serializer: Any,  # noqa: ANN401
    deserializer: Any,  # noqa: ANN401
    skip: Any,  # noqa: ANN401
    *,
    typed: bool,
    lock: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Build sync wrapper for cached decorator."""

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        key = _make_key(func, args, kwargs, key_maker, typed=typed)
        result = cache.get(key, _SENTINEL)
        if result is not _SENTINEL:
            return _deserialize(result, deserializer)
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
    cache: TTLCache,
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
    cache: TTLCache,
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
