"""Internal JSON helpers: use orjson when available, else stdlib json.

Resolved once at import to avoid per-call branching.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypeAlias

# Recursive aliases stay on `TypeAlias`, not PEP 695 `type`: the `type`
# keyword breaks recursive expansion in `ty`.
JSONEncodable: TypeAlias = (  # noqa: UP040
    str
    | int
    | float
    | bool
    | datetime
    | None
    | Mapping[str, "JSONEncodable"]
    | list["JSONEncodable"]
    | tuple["JSONEncodable", ...]
)
"""Recursive JSON-encodable value (``datetime`` becomes an ISO 8601 string)."""


JSONDecodable: TypeAlias = (  # noqa: UP040
    dict[str, Any] | list[Any] | str | int | float | bool | None
)
"""Value returned by ``json_loads``."""


def json_default(obj: object) -> str:
    """Encode ``datetime`` as ISO 8601 for stdlib json, else raise ``TypeError``."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    msg = f"Type is not JSON serializable: {type(obj).__name__}"
    raise TypeError(msg)


try:
    import orjson

    def json_dumps_bytes(obj: JSONEncodable) -> bytes:
        """Serialize object to JSON bytes using orjson."""
        return orjson.dumps(obj)

    def json_dumps_str(obj: JSONEncodable) -> str:
        """Serialize object to JSON string using orjson."""
        return orjson.dumps(obj).decode("utf-8")

    def json_loads(data: bytes | str) -> JSONDecodable:
        """Deserialize JSON bytes or string using orjson."""
        return orjson.loads(data)

    _HAS_ORJSON = True
except ImportError:
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

    _HAS_ORJSON = False


def has_orjson() -> bool:
    """Check if orjson is available."""
    return _HAS_ORJSON
