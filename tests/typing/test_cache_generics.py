"""Static typing samples for cache generic inference.

Runs as a pytest module so the imports execute, and is also picked up
by `uv run ty check` so the `assert_type` calls validate that
`TTLCache[T]`, generic serializers, and `JsonSerializer` keep
composing end-to-end. A regression that widens inference back to
`Any` fails ty even when all runtime tests pass.
"""

from __future__ import annotations

from typing import assert_type

from pydantic import BaseModel

from grelmicro._json import JSONDecodable
from grelmicro.cache import serializers
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.ttl import TTLCache


class _User(BaseModel):
    id: int
    name: str


async def test_binary_serializer_preserves_t() -> None:
    """`TTLCache[int]` with a generic serializer keeps `int | None` on get."""
    cache = TTLCache[int](
        ttl=60,
        serializer=serializers.PickleSerializer[int](),
        backend=MemoryCacheAdapter(),
    )
    await cache.set("k", 1)
    value = await cache.get("k")
    assert_type(value, int | None)


async def test_pydantic_serializer_preserves_t() -> None:
    """`TTLCache[User]` with `PydanticSerializer(User)` keeps `User | None` on get."""
    cache = TTLCache[_User](
        ttl=60,
        serializer=serializers.PydanticSerializer(_User),
        backend=MemoryCacheAdapter(),
    )
    await cache.set("k", _User(id=1, name="alice"))
    value = await cache.get("k")
    assert_type(value, _User | None)


async def test_json_serializer_typed_cache_returns_json_decodable() -> None:
    """`TTLCache[JSONDecodable]` with `JsonSerializer` keeps `JSONDecodable | None`."""
    cache: TTLCache[JSONDecodable] = TTLCache(
        ttl=60,
        serializer=serializers.JsonSerializer(),
        backend=MemoryCacheAdapter(),
    )
    await cache.set("k", {"a": 1})
    value = await cache.get("k")
    assert_type(value, JSONDecodable | None)


async def test_default_serializer_get_returns_bytes_or_none() -> None:
    """`TTLCache[bytes]` with no serializer keeps `bytes | None` on get."""
    cache = TTLCache[bytes](ttl=60, backend=MemoryCacheAdapter())
    await cache.set("k", b"raw")
    value = await cache.get("k")
    assert_type(value, bytes | None)
