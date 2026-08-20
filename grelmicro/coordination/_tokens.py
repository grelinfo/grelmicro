"""Coordination Tokens."""

import os
from asyncio import current_task
from itertools import count
from secrets import token_hex
from threading import local
from uuid import UUID

_guard_counter = count()

_origin_pid = os.getpid()
"""The process that imported this module, and so minted the identities."""

_fork_suffixes: dict[int, str] = {}
"""One suffix per process that inherited an identity, keyed by its pid.

`setdefault` on a dict is atomic, so two threads racing the first token
build after a fork settle on one suffix. Building two would be worse than
building none: a token stamped with the first would no longer match the
ownership check built with the second, and the holder could not release
its own lock.
"""


def resolve_worker(worker: UUID | str) -> str:
    """Return the worker identity, diverged when this process was forked.

    A pre-fork server such as `gunicorn --preload` builds the application
    once and forks, so every child inherits the same identity. Two workers
    would then present the same lock token and one could release or extend
    a lease the other holds, and every child would read the leader record
    holder as itself and lead at the same time.

    The pid is compared on every call rather than hooked with
    `os.register_at_fork`, because a hook only fires for a fork taken
    through Python after this module was imported. Comparing covers the
    rest: a process restored from a checkpoint, or duplicated from an
    image that already carried the identity.
    """
    pid = os.getpid()
    if pid == _origin_pid:
        return str(worker)
    suffix = _fork_suffixes.get(pid)
    if suffix is None:
        suffix = _fork_suffixes.setdefault(pid, f".{token_hex(4)}")
    return f"{worker}{suffix}"


def generate_worker_id() -> str:
    """Generate a unique worker identity (16 random hex chars, 64 bits)."""
    return token_hex(8)


class HolderIdentity:
    """A holder's own identity, minted once and living exactly as long as it.

    Neither a thread ident nor an object `id()` is an identity: CPython
    hands both to the next holder as soon as the previous one is gone, and
    a lock token built from one would let that next holder release,
    extend, or take a lease it never acquired.

    Weak-referenceable, so a caller can key its bookkeeping on the identity
    and have a holder that exits without releasing drop out on its own.
    """

    __slots__ = ("__weakref__", "value")

    def __init__(self) -> None:
        """Mint the identity."""
        self.value = f"{next(_guard_counter)}.{token_hex(8)}"

    def __repr__(self) -> str:
        """Return the minted value."""
        return f"HolderIdentity({self.value!r})"


_TASK_SLOT = "_grelmicro_identity"
"""Attribute the task carries its identity on, released with the task."""

_thread_identity = local()
"""Each thread's own identity, released by the interpreter with the thread.

Deliberately not keyed on the `Thread` object. A thread the `threading`
module did not create shares one cached `_DummyThread` with every later
thread that lands on the same recycled ident, so keying on it would give
them all one identity, and that dummy is pinned for the life of the
process so it would never be released either.

Each thread reads and writes only its own slot, so the first mint needs no
lock.
"""


def current_thread_identity() -> HolderIdentity:
    """Return the calling thread's identity, minting it on first use."""
    identity: HolderIdentity | None = getattr(_thread_identity, "value", None)
    if identity is None:
        identity = HolderIdentity()
        _thread_identity.value = identity
    return identity


def _task_identity() -> HolderIdentity:
    """Return the running task's identity, minting it on first use.

    Held on the task, so it is released with it. A task only ever mints its
    own identity, and it does so on the loop, so the first mint needs no
    lock either.
    """
    task = current_task()
    if task is None:  # pragma: no cover
        msg = "a task token must be built from a running asyncio task"
        raise RuntimeError(msg)
    identity: HolderIdentity | None = getattr(task, _TASK_SLOT, None)
    if identity is None:
        identity = HolderIdentity()
        setattr(task, _TASK_SLOT, identity)
    return identity


def generate_task_token(worker: UUID | str, nonce: str = "") -> str:
    """Generate a task token from the worker and the task's own identity."""
    return f"{resolve_worker(worker)}:task:{_task_identity().value}{nonce}"


def generate_token_nonce() -> str:
    """Generate a unique, unpredictable token nonce suffix.

    Combines a process-local counter with random bytes (e.g. ':0.a1b2c3d4').
    The counter is unique across handles in the same process and the random
    part is unguessable.

    Thread-safe: ``next()`` on ``itertools.count`` is a single C-level
    operation protected by the GIL.
    """
    return f":{next(_guard_counter)}.{token_hex(8)}"


def generate_thread_token(
    worker: UUID | str,
    nonce: str = "",
    *,
    identity: HolderIdentity | None = None,
) -> str:
    """Generate a thread token from the worker and the thread's own identity.

    `identity` defaults to the calling thread's. A method that runs on the
    event loop on behalf of a worker thread has to carry that thread's
    identity in, because the calling thread there is the loop.
    """
    if identity is None:
        identity = current_thread_identity()
    return f"{resolve_worker(worker)}:thread:{identity.value}{nonce}"
