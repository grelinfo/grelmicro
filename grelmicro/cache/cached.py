"""Cached Decorator."""

import asyncio
import functools
from collections.abc import Callable
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
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Cache decorator for sync and async functions.

    Automatically detects whether the decorated function is sync or
    async and wraps it accordingly. Cached values are stored in the
    provided ``TTLCache`` instance.

    The decorated function exposes ``cache_info()`` and
    ``cache_clear()`` helpers (matching ``functools.lru_cache``).

    Does not provide stampede / thundering-herd protection: when a
    cache entry expires, concurrent callers may all execute the
    underlying function simultaneously.

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

            @functools.wraps(func)
            async def async_wrapper(
                *args: P.args,
                **kwargs: P.kwargs,
            ) -> R:
                key = _make_key(func, args, kwargs, key_maker, typed=typed)
                result = cache.get(key, _SENTINEL)
                if result is not _SENTINEL:
                    return _deserialize(result, deserializer)
                result = await func(*args, **kwargs)
                if skip is None or not skip(result):
                    cache.set(key, _serialize(result, serializer))
                return result

            async_wrapper.cache_info = cache.cache_info  # type: ignore[attr-defined]
            async_wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(
            *args: P.args,
            **kwargs: P.kwargs,
        ) -> R:
            key = _make_key(func, args, kwargs, key_maker, typed=typed)
            result = cache.get(key, _SENTINEL)
            if result is not _SENTINEL:
                return _deserialize(result, deserializer)
            result = func(*args, **kwargs)
            if skip is None or not skip(result):
                cache.set(key, _serialize(result, serializer))
            return result

        sync_wrapper.cache_info = cache.cache_info  # type: ignore[attr-defined]
        sync_wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        return sync_wrapper

    return decorator


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
