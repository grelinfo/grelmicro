"""Payload codec.

Encodes a payload model to a JSON-native dict for `jsonb` storage and
validates it back. Built on Pydantic's `TypeAdapter`, the same engine
`grelmicro.cache.serializers.PydanticSerializer` uses. Cache stores opaque
bytes, so it serializes to bytes. The outbox stores queryable `jsonb`, so it
serializes to a dict. Both share the `TypeAdapter` roundtrip, so any
`BaseModel`, dataclass, or `TypedDict` works as a payload.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping


@lru_cache(maxsize=512)
def _adapter(model: type[Any]) -> TypeAdapter[Any]:
    """Return a cached `TypeAdapter` for a payload model.

    Cached so the adapter is built once per model, not per message, keeping
    the publish and delivery paths hot.
    """
    return TypeAdapter(model)


def encode_payload(value: object) -> dict[str, Any]:
    """Encode a payload model to a JSON-native dict."""
    return _adapter(type(value)).dump_python(value, mode="json")


def decode_payload(model: type[Any], data: Mapping[str, Any]) -> Any:  # noqa: ANN401
    """Validate a dict back into a payload model."""
    return _adapter(model).validate_python(data)
