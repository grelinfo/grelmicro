"""Synchronization Tokens."""

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
    """Generate a unique token nonce suffix (e.g., ':0', ':1').

    Thread-safe: ``next()`` on ``itertools.count`` is a single C-level
    operation protected by the GIL.
    """
    return f":{next(_guard_counter)}"


def generate_thread_token(worker: UUID | str, nonce: str = "") -> str:
    """Generate a thread token from the worker identity and the current thread ID."""
    thread_id = get_ident()
    return f"{worker}:thread:{thread_id}{nonce}"
