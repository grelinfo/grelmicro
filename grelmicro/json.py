"""JSON serialization utilities.

Fast JSON encoding and decoding using ``orjson`` when available,
with automatic fallback to the standard library ``json`` module.

``orjson`` is roughly 7x faster than stdlib ``json`` and is included
in the ``grelmicro[standard]`` extra.

Example::

    from grelmicro.cache import TTLCache, JsonSerializer

    cache = TTLCache(ttl=300, serializer=JsonSerializer())
"""

from grelmicro._json import (
    JSONDecodable,
    JSONEncodable,
    has_orjson,
    json_default,
    json_dumps_bytes,
    json_dumps_str,
    json_loads,
)

__all__ = [
    "JSONDecodable",
    "JSONEncodable",
    "has_orjson",
    "json_default",
    "json_dumps_bytes",
    "json_dumps_str",
    "json_loads",
]
