"""Tests for the in-memory leader election backend."""

import asyncio

import pytest

from grelmicro.coordination.memory import MemoryLeaderElectionAdapter

pytestmark = [pytest.mark.timeout(1)]

NAME = "svc"
SHORT = 0.02
EXPIRE = 0.05
W1 = "w1"
W2 = "w2"


async def test_acquire_fresh_starts_at_zero_transitions() -> None:
    """A first acquisition holds the lease with zero transitions."""
    backend = MemoryLeaderElectionAdapter()
    record = await backend.acquire_or_renew(
        name=NAME, token=W1, duration=10, metadata={"pod": "a"}
    )
    assert record.holder == "w1"
    assert record.transitions == 0
    assert record.metadata == {"pod": "a"}
    assert record.acquired_at == record.renewed_at


async def test_renew_same_holder_keeps_acquired_and_transitions() -> None:
    """Renewing as the same holder moves renewed_at but not acquired/transitions."""
    backend = MemoryLeaderElectionAdapter()
    first = await backend.acquire_or_renew(name=NAME, token=W1, duration=10)
    second = await backend.acquire_or_renew(
        name=NAME, token=W1, duration=10, metadata={"v": "2"}
    )
    assert second.acquired_at == first.acquired_at
    assert second.transitions == 0
    assert second.renewed_at >= first.renewed_at
    assert second.metadata == {"v": "2"}


async def test_live_lease_blocks_a_different_holder() -> None:
    """A different worker cannot take a live lease and sees the holder's record."""
    backend = MemoryLeaderElectionAdapter()
    await backend.acquire_or_renew(name=NAME, token=W1, duration=10)
    record = await backend.acquire_or_renew(name=NAME, token=W2, duration=10)
    assert record.holder == "w1"


async def test_takeover_after_expiry_increments_transitions() -> None:
    """Once the lease expires a different holder takes over and bumps transitions."""
    backend = MemoryLeaderElectionAdapter()
    await backend.acquire_or_renew(name=NAME, token=W1, duration=SHORT)
    await asyncio.sleep(EXPIRE)
    record = await backend.acquire_or_renew(name=NAME, token=W2, duration=10)
    assert record.holder == "w2"
    assert record.transitions == 1


async def test_reacquire_own_expired_lease_keeps_transitions() -> None:
    """Reacquiring your own expired lease does not count as a transition."""
    backend = MemoryLeaderElectionAdapter()
    await backend.acquire_or_renew(name=NAME, token=W1, duration=SHORT)
    await asyncio.sleep(EXPIRE)
    record = await backend.acquire_or_renew(name=NAME, token=W1, duration=10)
    assert record.holder == "w1"
    assert record.transitions == 0


async def test_release_returns_true_for_holder_false_otherwise() -> None:
    """Release succeeds only for the current holder."""
    backend = MemoryLeaderElectionAdapter()
    await backend.acquire_or_renew(name=NAME, token=W1, duration=10)
    assert await backend.release(name=NAME, token=W2) is False
    assert await backend.release(name=NAME, token=W1) is True
    assert await backend.get(name=NAME) is None


async def test_get_returns_none_after_expiry() -> None:
    """`get` reports no leader once the lease has expired."""
    backend = MemoryLeaderElectionAdapter()
    await backend.acquire_or_renew(name=NAME, token=W1, duration=SHORT)
    assert await backend.get(name=NAME) is not None
    await asyncio.sleep(EXPIRE)
    assert await backend.get(name=NAME) is None


async def test_context_manager() -> None:
    """The backend works as an async context manager."""
    async with MemoryLeaderElectionAdapter() as backend:
        record = await backend.acquire_or_renew(
            name=NAME, token=W1, duration=10
        )
    assert record.holder == "w1"
