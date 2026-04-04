"""Shared JSON serialization utilities.

Provides fast JSON encoding using ``orjson`` when available,
falling back to the standard library ``json`` module.

The implementation is resolved once at import time to avoid
per-call branching.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypeAlias

JSONEncodable: TypeAlias = (
    Mapping[str, Any]
    | list[Any]
    | tuple[Any, ...]
    | str
    | int
    | float
    | bool
    | datetime
    | None
)
"""Types accepted by ``json_dumps_bytes`` and ``json_dumps_str``."""

JSONDecodable: TypeAlias = (
    dict[str, Any] | list[Any] | str | int | float | bool | None
)
"""Types returned by ``json_loads``."""

try:
    import orjson
except ImportError:
    orjson: Any = None  # type: ignore[no-redef]


def json_default(obj: object) -> str:
    """Handle non-serializable types for stdlib json.

    Converts ``datetime`` instances to ISO 8601 strings.
    Raises ``TypeError`` for all other non-serializable types.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    msg = f"Type is not JSON serializable: {type(obj).__name__}"
    raise TypeError(msg)


def has_orjson() -> bool:
    """Check if orjson is available."""
    return orjson is not None


if orjson is not None:

    def json_dumps_bytes(obj: JSONEncodable) -> bytes:
        """Serialize object to JSON bytes using orjson."""
        return orjson.dumps(obj)  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]

    def json_dumps_str(obj: JSONEncodable) -> str:
        """Serialize object to JSON string using orjson."""
        return orjson.dumps(obj).decode("utf-8")  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]

    def json_loads(data: bytes | str) -> JSONDecodable:
        """Deserialize JSON bytes or string using orjson."""
        return orjson.loads(data)  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]

else:
    import json

    def json_dumps_bytes(obj: JSONEncodable) -> bytes:
        """Serialize object to JSON bytes using stdlib json."""
        return json.dumps(
            obj, separators=(",", ":"), default=json_default
        ).encode("utf-8")

    def json_dumps_str(obj: JSONEncodable) -> str:
        """Serialize object to JSON string using stdlib json."""
        return json.dumps(obj, separators=(",", ":"), default=json_default)

    def json_loads(data: bytes | str) -> JSONDecodable:
        """Deserialize JSON bytes or string using stdlib json."""
        return json.loads(data)  # type: ignore[return-value]
