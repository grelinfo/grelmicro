"""Time-ordered UUIDv7 generation (RFC 9562)."""

from __future__ import annotations

import os
import time
from uuid import UUID


def uuid7() -> UUID:
    """Return a time-ordered UUIDv7.

    The first 48 bits hold a Unix millisecond timestamp, so ids sort by
    creation time. The remaining bits are random. Sorting or indexing by
    the id therefore tracks insertion order and keeps B-tree writes local.
    """
    timestamp_ms = time.time_ns() // 1_000_000
    value = bytearray(timestamp_ms.to_bytes(6, "big") + os.urandom(10))
    value[6] = (value[6] & 0x0F) | 0x70
    value[8] = (value[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(value))
