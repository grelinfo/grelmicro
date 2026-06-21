"""Validation-boundary tests for the `@cached` decorator.

The broader suite checks the common decorator paths. These pin the `early` and
`stale_ttl` validation boundaries, so a flipped comparison (`0 <= early` to
`0 < early`, `stale_ttl <= 0` to `<= 1`) is caught at decoration time.
"""

from __future__ import annotations

from grelmicro.cache.cached import cached

_TTL = 60


def test_early_zero_is_accepted() -> None:
    """`early=0.0` is a valid lower bound (the guard allows `0 <= early`)."""

    @cached(ttl=_TTL, early=0.0)
    async def fn() -> int:
        return 1

    assert fn is not None


def test_stale_ttl_of_one_is_accepted() -> None:
    """`stale_ttl=1` is valid (the guard rejects `<= 0`, not `<= 1`)."""

    @cached(ttl=_TTL, stale_ttl=1)
    async def fn() -> int:
        return 1

    assert fn is not None
