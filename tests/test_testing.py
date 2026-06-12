"""Tests for the protocol-level call recorder."""

import pytest

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.testing import Call, CallLog, record

pytestmark = [pytest.mark.timeout(1)]

T1 = "t1"
T2 = "t2"
_CONSTANT = 7


class _FakeBackend:
    """A backend-like object with a mix of members to instrument."""

    constant = _CONSTANT

    def __init__(self) -> None:
        self.released: list[str] = []

    async def acquire(self, *, name: str, token: str) -> bool:
        return bool(name and token)

    async def release(self, *, name: str) -> bool:
        self.released.append(name)
        return bool(name)

    def sync_helper(self) -> str:
        return "not recorded"

    async def _private(self) -> None:
        return None


async def test_records_public_async_calls() -> None:
    """Each public async call is recorded with its keyword arguments."""
    backend = _FakeBackend()
    log = record(backend)

    await backend.acquire(name="cart", token=T1)
    await backend.release(name="cart")

    assert log.methods() == ["acquire", "release"]
    assert log.calls[0] == Call("acquire", (), {"name": "cart", "token": "t1"})


async def test_forwards_to_the_original_method() -> None:
    """Recording does not change behavior: the call still runs."""
    backend = _FakeBackend()
    record(backend)

    result = await backend.acquire(name="cart", token=T1)
    await backend.release(name="cart")

    assert result is True
    assert backend.released == ["cart"]


async def test_does_not_wrap_sync_or_private_members() -> None:
    """Only public coroutine methods are instrumented."""
    backend = _FakeBackend()
    log = record(backend)

    assert backend.sync_helper() == "not recorded"
    await backend._private()
    assert backend.constant == _CONSTANT
    assert log.calls == []


async def test_count_filters_by_method_and_kwargs() -> None:
    """`count` matches on method name and keyword arguments."""
    backend = _FakeBackend()
    log = record(backend)

    await backend.acquire(name="cart", token=T1)
    await backend.acquire(name="order", token=T2)

    expected = 2
    assert log.count() == expected
    assert log.count("acquire") == expected
    assert log.count("acquire", name="cart") == 1
    assert log.count("acquire", name="missing") == 0
    assert log.count("release") == 0


async def test_reset_clears_recorded_calls() -> None:
    """`reset` drops the recorded history."""
    backend = _FakeBackend()
    log = record(backend)

    await backend.acquire(name="cart", token=T1)
    log.reset()

    assert log.count() == 0


def test_call_log_default_is_empty() -> None:
    """A fresh `CallLog` records nothing."""
    assert CallLog().calls == []


class _PosBackend:
    """Backend with a method that accepts positional arguments."""

    async def fetch(self, resource: str, *, version: int = 1) -> str:
        return f"{resource}:{version}"


async def test_records_positional_args() -> None:
    """Positional arguments appear in `call.args`, keyword args in `call.kwargs`."""
    backend = _PosBackend()
    log = record(backend)

    await backend.fetch("items", version=2)

    assert len(log.calls) == 1
    assert log.calls[0].args == ("items",)
    assert log.calls[0].kwargs == {"version": 2}


async def test_count_filters_by_args() -> None:
    """`count` with `args=` matches only calls whose positional args match."""
    backend = _PosBackend()
    log = record(backend)

    await backend.fetch("items")
    await backend.fetch("orders")

    expected = 2
    assert log.count("fetch", args=("items",)) == 1
    assert log.count("fetch", args=("orders",)) == 1
    assert log.count("fetch", args=("missing",)) == 0
    assert log.count("fetch") == expected


async def test_records_calls_through_a_component() -> None:
    """A recorded backend works unchanged inside a `Grelmicro` app."""
    backend = MemoryLockAdapter()
    log = record(backend)
    micro = Grelmicro(uses=[Coordination(lock=backend)])

    async with micro, micro.coordination.lock("cart"):
        pass

    assert log.count("acquire") == 1
    assert log.count("release") == 1
