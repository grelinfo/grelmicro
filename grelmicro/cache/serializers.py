"""Cache Serializers.

Pluggable serialization strategies for ``TTLCache``.

Example::

    from grelmicro.cache import TTLCache
    from grelmicro.cache.serializers import (
        JsonSerializer,
        PydanticSerializer,
        PickleSerializer,
    )

    # JSON (orjson when available)
    cache = TTLCache(ttl=300, serializer=JsonSerializer())

    # Pydantic model (TypeAdapter, fastest)
    cache = TTLCache[User](ttl=300, serializer=PydanticSerializer(User))

    # Pickle (supports any Python object)
    cache = TTLCache(ttl=300, serializer=PickleSerializer())
"""

from __future__ import annotations

import pickle
from typing import Any, Generic, Protocol, runtime_checkable

from pydantic import TypeAdapter
from typing_extensions import TypeVar

from grelmicro._json import (
    JSONDecodable,
    JSONEncodable,
    json_dumps_bytes,
    json_loads,
)

T = TypeVar("T", default=Any)


@runtime_checkable
class CacheSerializer(Protocol[T]):
    """Protocol for cache serialization strategies.

    Any object implementing ``dumps`` and ``loads`` can be used
    as a ``TTLCache`` serializer.
    """

    def dumps(self, value: T) -> bytes:
        """Serialize a value to bytes."""
        ...

    def loads(self, data: bytes) -> T:
        """Deserialize bytes to a value."""
        ...


class PickleSerializer(Generic[T]):
    """Serialize values using Python pickle.

    Supports any picklable Python object. Fast and transparent,
    but produces opaque binary data.

    Danger:
        Deserialization can execute arbitrary code. A compromised cache
        backend can run code inside the application process. Use this
        serializer only when the backend is fully trusted (in-process,
        single-tenant). For shared backends (Redis, Memcached, any
        multi-tenant store), use ``JsonSerializer`` or
        ``PydanticSerializer`` instead.

    Args:
        protocol: Serialization protocol version. Defaults to the
            highest available protocol.
    """

    def __init__(self, *, protocol: int = pickle.HIGHEST_PROTOCOL) -> None:
        """Initialize the pickle serializer."""
        self._protocol = protocol

    def dumps(self, value: T) -> bytes:
        """Serialize a value to bytes."""
        return pickle.dumps(value, protocol=self._protocol)

    def loads(self, data: bytes) -> T:
        """Deserialize bytes to a value."""
        return pickle.loads(data)  # noqa: S301


class JsonSerializer:
    """Serialize values as JSON bytes.

    Uses ``orjson`` when available (roughly 7x faster than stdlib),
    otherwise falls back to the standard library ``json`` module.

    Suitable for dicts, lists, and other JSON-native types.
    ``datetime`` objects are serialized to ISO 8601 strings but
    deserialized back as strings (not ``datetime``).
    """

    def dumps(self, value: JSONEncodable) -> bytes:
        """Serialize a value to JSON bytes."""
        return json_dumps_bytes(value)

    def loads(self, data: bytes) -> JSONDecodable:
        """Deserialize JSON bytes to a value."""
        return json_loads(data)


class PydanticSerializer(Generic[T]):
    """Serialize values using Pydantic's TypeAdapter.

    Uses Pydantic's Rust-based serializer for fast, type-safe
    roundtrips. Works with ``BaseModel``, ``dataclass``,
    ``TypedDict``, and any type supported by ``TypeAdapter``.

    This is the fastest serialization option (benchmarked at
    roughly 2x faster than pickle for Pydantic models).

    Args:
        model: The type to serialize/deserialize. Can be any type
            supported by ``pydantic.TypeAdapter``.
    """

    def __init__(self, model: type[T]) -> None:
        """Initialize the Pydantic serializer."""
        self._adapter: TypeAdapter[T] = TypeAdapter(model)

    def dumps(self, value: T) -> bytes:
        """Serialize a value to JSON bytes via TypeAdapter."""
        return self._adapter.dump_json(value)

    def loads(self, data: bytes) -> T:
        """Deserialize JSON bytes to a typed value via TypeAdapter."""
        return self._adapter.validate_json(data)
