"""Tests for the in-memory Schedule Adapter."""

import asyncio

import pytest

from grelmicro.coordination.memory import MemoryScheduleAdapter

pytestmark = [pytest.mark.timeout(5)]

OLD = 100.0
NEW = 160.0
OTHER = 200.0


async def test_last_fired_is_none_before_any_claim() -> None:
    """`last_fired` is `None` for a never-claimed name."""
    async with MemoryScheduleAdapter() as backend:
        assert await backend.last_fired("job") is None


async def test_claim_sets_last_fired() -> None:
    """A first claim stores the due epoch and returns `True`."""
    async with MemoryScheduleAdapter() as backend:
        won = await backend.claim("job", OLD)
        assert won is True
        assert await backend.last_fired("job") == OLD


async def test_claim_advances_to_a_newer_due() -> None:
    """A claim with a strictly greater due wins and advances the state."""
    async with MemoryScheduleAdapter() as backend:
        await backend.claim("job", OLD)
        won = await backend.claim("job", NEW)
        assert won is True
        assert await backend.last_fired("job") == NEW


async def test_claim_rejects_an_equal_due() -> None:
    """Claiming the same due twice wins only once."""
    async with MemoryScheduleAdapter() as backend:
        assert await backend.claim("job", OLD) is True
        assert await backend.claim("job", OLD) is False
        assert await backend.last_fired("job") == OLD


async def test_claim_rejects_an_older_due() -> None:
    """A claim with an older due loses and leaves the state untouched."""
    async with MemoryScheduleAdapter() as backend:
        await backend.claim("job", NEW)
        won = await backend.claim("job", OLD)
        assert won is False
        assert await backend.last_fired("job") == NEW


async def test_concurrent_claims_only_one_wins() -> None:
    """Many concurrent claims of one due elect a single winner."""
    async with MemoryScheduleAdapter() as backend:
        results = await asyncio.gather(
            *(backend.claim("job", OLD) for _ in range(20))
        )
        assert results.count(True) == 1


async def test_names_are_independent() -> None:
    """Each schedule name keeps its own last-fire state."""
    async with MemoryScheduleAdapter() as backend:
        await backend.claim("a", OLD)
        await backend.claim("b", OTHER)
        assert await backend.last_fired("a") == OLD
        assert await backend.last_fired("b") == OTHER


async def test_exit_clears_state() -> None:
    """Closing the backend clears the stored fires."""
    backend = MemoryScheduleAdapter()
    async with backend:
        await backend.claim("job", OLD)
    assert await backend.last_fired("job") is None
