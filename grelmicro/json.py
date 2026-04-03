"""JSON serialization utilities.

Fast JSON encoding and decoding using ``orjson`` when available,
with automatic fallback to the standard library ``json`` module.

``orjson`` is roughly 7x faster than stdlib ``json`` and is included
in the ``grelmicro[standard]`` extra.

Example::

    from grelmicro.json import json_dumps_bytes, json_loads_bytes
    from grelmicro.cache import TTLCache

    cache = TTLCache(
        ttl=300,
        serializer=json_dumps_bytes,
        deserializer=json_loads_bytes,
    )
"""

from grelmicro._json import (
    JSONSerializable,
    has_orjson,
    json_default,
    json_dumps_bytes,
    json_dumps_str,
    json_loads_bytes,
)

__all__ = [
    "JSONSerializable",
    "has_orjson",
    "json_default",
    "json_dumps_bytes",
    "json_dumps_str",
    "json_loads_bytes",
]
