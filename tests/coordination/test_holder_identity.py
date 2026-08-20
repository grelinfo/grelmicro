"""Two different holders must never present the same ownership token.

A lock token is what the backend compares to decide who holds a lease, so
two holders that mint the same one can release, extend, or take each
other's lock. CPython recycles both a thread ident and an object `id()`
as soon as the previous owner is gone, so neither is an identity.
"""

import _thread
import asyncio
import gc
import threading
import time
import weakref

import pytest

from grelmicro.coordination import Lock, ReadWriteLock
from grelmicro.coordination._tokens import (
    current_thread_identity,
    generate_task_token,
    generate_thread_token,
)
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.coordination.memory import (
    MemoryLockAdapter,
    MemoryReadWriteLockAdapter,
)
from grelmicro.errors import WouldBlockError

_ROUNDS = 6
_LEASE = 60
_FOREIGN_TIMEOUT = 5.0
_FOREIGN_SETTLE = 0.01


def test_threads_never_share_a_token() -> None:
    """Each thread mints its own token, even when the ident is recycled."""
    tokens: list[str] = []
    idents: set[int] = set()

    def run() -> None:
        idents.add(threading.get_ident())
        tokens.append(generate_thread_token("worker"))

    for _ in range(_ROUNDS):
        thread = threading.Thread(target=run)
        thread.start()
        thread.join()

    assert len(set(tokens)) == _ROUNDS
    if len(idents) == _ROUNDS:
        pytest.skip(
            "this runtime did not recycle a thread ident, so the assertion "
            "above held without being put under the pressure it exists for"
        )


async def test_tasks_never_share_a_token() -> None:
    """Each task mints its own token, even when the object id is recycled."""
    tokens: list[str] = []

    async def holder() -> None:
        tokens.append(generate_task_token("worker"))

    for _ in range(_ROUNDS):
        await asyncio.create_task(holder())
        gc.collect()

    assert len(set(tokens)) == _ROUNDS


def test_one_thread_keeps_its_token_across_calls() -> None:
    """A holder must reproduce its own token, or it cannot release."""
    tokens: list[str] = []

    def run() -> None:
        tokens.append(generate_thread_token("worker"))
        tokens.append(generate_thread_token("worker"))

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()

    assert tokens[0] == tokens[1]


async def test_a_dead_thread_does_not_hand_its_lock_to_the_next() -> None:
    """A thread that died holding must not admit the next thread.

    The lease is still held, so the next thread has to be refused. Keying
    ownership on the recycled ident told it either that it already held
    the lock or, worse, granted it.
    """
    backend = MemoryLockAdapter()
    lock = Lock("cart", backend=backend, lease_duration=_LEASE)
    loop = asyncio.get_running_loop()
    outcome: list[type[BaseException] | None] = []

    def dies_holding() -> None:
        lock.from_thread.acquire()

    def next_thread() -> None:
        try:
            lock.from_thread.acquire_nowait()
        except BaseException as exc:  # noqa: BLE001
            outcome.append(type(exc))
        else:
            outcome.append(None)

    async with backend:
        first = threading.Thread(target=dies_holding)
        first.start()
        await loop.run_in_executor(None, first.join)

        second = threading.Thread(target=next_thread)
        second.start()
        await loop.run_in_executor(None, second.join)

    assert outcome == [WouldBlockError]


async def test_a_dead_thread_cannot_be_released_by_the_next() -> None:
    """The next thread must not be able to release a lease it never took."""
    backend = MemoryLockAdapter()
    lock = Lock("cart", backend=backend, lease_duration=_LEASE)
    loop = asyncio.get_running_loop()
    outcome: list[type[BaseException] | None] = []

    def dies_holding() -> None:
        lock.from_thread.acquire()

    def next_thread() -> None:
        try:
            lock.from_thread.release()
        except BaseException as exc:  # noqa: BLE001
            outcome.append(type(exc))
        else:
            outcome.append(None)

    async with backend:
        first = threading.Thread(target=dies_holding)
        first.start()
        await loop.run_in_executor(None, first.join)

        second = threading.Thread(target=next_thread)
        second.start()
        await loop.run_in_executor(None, second.join)

    assert outcome == [LockNotOwnedError]


@pytest.mark.parametrize("mode", ["read", "write"])
async def test_read_write_lock_refuses_a_recycled_ident(mode: str) -> None:
    """`ReadWriteLock` keys its thread guards the same way, so it is checked too."""
    backend = MemoryReadWriteLockAdapter()
    lock = ReadWriteLock("report", backend=backend, lease_duration=_LEASE)
    side = lock.write if mode == "write" else lock.read
    loop = asyncio.get_running_loop()
    outcome: list[type[BaseException] | None] = []

    def dies_holding() -> None:
        side.from_thread.acquire()

    def next_thread() -> None:
        try:
            side.from_thread.release()
        except BaseException as exc:  # noqa: BLE001
            outcome.append(type(exc))
        else:
            outcome.append(None)

    async with backend:
        first = threading.Thread(target=dies_holding)
        first.start()
        await loop.run_in_executor(None, first.join)

        second = threading.Thread(target=next_thread)
        second.start()
        await loop.run_in_executor(None, second.join)

    assert outcome != [None], "the next thread released a lease it never took"


def test_foreign_threads_never_share_an_identity() -> None:
    """A thread the `threading` module did not create still gets its own.

    `current_thread()` hands every such thread one cached `_DummyThread`,
    so an identity keyed on that object would be shared by all of them and
    would never be released. The identity lives in a `threading.local`
    instead, which the interpreter clears when the real thread ends.
    """
    values: list[str] = []
    refs: list[weakref.ref[object]] = []
    done = threading.Event()

    def foreign() -> None:
        identity = current_thread_identity()
        values.append(identity.value)
        refs.append(weakref.ref(identity))
        done.set()

    for _ in range(_ROUNDS):
        done.clear()
        _thread.start_new_thread(foreign, ())
        assert done.wait(_FOREIGN_TIMEOUT)
        time.sleep(_FOREIGN_SETTLE)

    assert len(set(values)) == _ROUNDS

    # Poll rather than sleep a fixed margin: `done.set()` runs before the
    # thread returns, so a loaded runner can still be tearing it down.
    deadline = time.monotonic() + _FOREIGN_TIMEOUT
    while time.monotonic() < deadline:
        gc.collect()
        alive = [ref for ref in refs if ref() is not None]
        if not alive:
            break
        time.sleep(_FOREIGN_SETTLE)
    assert alive == []


def test_an_identity_shows_its_value() -> None:
    """Ownership bookkeeping is readable when a lock is inspected."""
    identity = current_thread_identity()

    assert repr(identity) == f"HolderIdentity({identity.value!r})"
