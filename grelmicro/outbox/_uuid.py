"""Time-ordered UUIDv7 generation (RFC 9562).

On Python 3.14+ this re-exports the standard library `uuid.uuid7`, which
is monotonic within a process: a counter fills the sub-millisecond bits,
so ids minted in the same millisecond still sort in creation order. On
3.12 and 3.13 a vendored implementation fills those bits with random
data, so ids from the same millisecond may sort out of order.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 14):  # pragma: no cover
    from uuid import uuid7
else:
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


__all__ = ["uuid7"]
