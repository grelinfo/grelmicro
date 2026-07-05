"""Coordination Tokens."""

from asyncio import current_task
from itertools import count
from secrets import token_hex
from threading import get_ident
from uuid import UUID

_guard_counter = count()


def generate_worker_id() -> str:
    """Generate a unique worker identity (16 random hex chars, 64 bits)."""
    return token_hex(8)


def generate_task_token(worker: UUID | str, nonce: str = "") -> str:
    """Generate a task token from the worker identity and the async task ID."""
    task = current_task()
    if task is None:  # pragma: no cover
        msg = "generate_task_token must be called from a running asyncio task"
        raise RuntimeError(msg)
    return f"{worker}:task:{id(task)}{nonce}"


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
    return f"{worker}:thread:{thread_id}{nonce}"
