"""Tests for the time-ordered UUIDv7 generator.

These pin the RFC 9562 layout the outbox relies on: version 7, the RFC
variant, and ids that sort by creation time across milliseconds. The same
guarantees hold whether the id comes from the vendored generator (3.12,
3.13) or the standard library `uuid.uuid7` (3.14+).
"""

from __future__ import annotations

import time

from grelmicro.outbox._uuid import uuid7

UUID_VERSION_7 = 7
RFC_VARIANT = 0b10


def test_uuid7_layout() -> None:
    """The id is a UUIDv7 with the RFC 9562 variant."""
    value = uuid7()
    assert value.version == UUID_VERSION_7
    assert (value.int >> 62) & 0b11 == RFC_VARIANT


def test_uuid7_sorts_by_creation_time() -> None:
    """Ids minted in separate milliseconds sort in creation order."""
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()
    assert first < second
    assert str(first) < str(second)
