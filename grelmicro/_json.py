"""Shared JSON serialization utilities.

Provides fast JSON encoding using ``orjson`` when available,
falling back to the standard library ``json`` module.

The implementation is resolved once at import time to avoid
per-call branching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

try:
    import orjson
except ImportError:
    orjson: Any = None  # type: ignore[no-redef]


def _json_default(obj: object) -> str:
    """Handle non-serializable types for stdlib json."""
    from datetime import datetime  # noqa: PLC0415

    if isinstance(obj, datetime):
        return obj.isoformat()
    msg = f"Type is not JSON serializable: {type(obj).__name__}"
    raise TypeError(msg)


def has_orjson() -> bool:
    """Check if orjson is available."""
    return orjson is not None


if orjson is not None:

    def json_dumps_bytes(obj: Mapping[str, Any] | dict[str, Any]) -> bytes:
        """Serialize object to JSON bytes using orjson."""
        return orjson.dumps(obj)  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]

    def json_dumps_str(obj: Mapping[str, Any] | dict[str, Any]) -> str:
        """Serialize object to JSON string using orjson."""
        return orjson.dumps(obj).decode("utf-8")  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]

else:
    import json

    def json_dumps_bytes(obj: Mapping[str, Any] | dict[str, Any]) -> bytes:
        """Serialize object to JSON bytes using stdlib json."""
        return json.dumps(
            obj, separators=(",", ":"), default=_json_default
        ).encode("utf-8")

    def json_dumps_str(obj: Mapping[str, Any] | dict[str, Any]) -> str:
        """Serialize object to JSON string using stdlib json."""
        return json.dumps(obj, separators=(",", ":"), default=_json_default)
