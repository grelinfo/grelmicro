"""Synchronization Tokens."""

from secrets import token_hex
from threading import get_ident
from uuid import UUID

from anyio import get_current_task


def generate_worker_id() -> str:
    """Generate a unique worker identity (8 random hex chars, 32 bits)."""
    return token_hex(4)


def generate_task_token(worker: UUID | str) -> str:
    """Generate a task token from the worker identity and the async task ID."""
    task_id = get_current_task().id
    return f"{worker}:task:{task_id}"


def generate_thread_token(worker: UUID | str) -> str:
    """Generate a thread token from the worker identity and the current thread ID."""
    thread_id = get_ident()
    return f"{worker}:thread:{thread_id}"
