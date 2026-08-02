"""Coordination Tokens."""

import os
from asyncio import current_task
from itertools import count
from secrets import token_hex
from threading import get_ident
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


def generate_task_token(worker: UUID | str, nonce: str = "") -> str:
    """Generate a task token from the worker identity and the async task ID."""
    task = current_task()
    if task is None:  # pragma: no cover
        msg = "generate_task_token must be called from a running asyncio task"
        raise RuntimeError(msg)
    return f"{resolve_worker(worker)}:task:{id(task)}{nonce}"


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
    worker: UUID | str, nonce: str = "", *, thread_id: int | None = None
) -> str:
    """Generate a thread token from the worker identity and a thread ID.

    The thread ID defaults to the current thread when not given.
    """
    if thread_id is None:
        thread_id = get_ident()
    return f"{resolve_worker(worker)}:thread:{thread_id}{nonce}"
