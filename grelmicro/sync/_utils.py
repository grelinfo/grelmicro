"""Synchronization Utilities."""

from threading import get_ident
from uuid import NAMESPACE_DNS, UUID, uuid1, uuid3

from anyio import get_current_task


def generate_worker_id() -> UUID:
    """Generate a unique worker identity using UUIDv1."""
    return uuid1()


def generate_worker_namespace(worker: str) -> UUID:
    """Generate a worker UUIDv3 namespace from the DNS namespace."""
    return uuid3(namespace=NAMESPACE_DNS, name=worker)


def generate_task_token(worker: UUID | str) -> str:
    """Generate a task token using UUIDv3 with the worker namespace and the async task ID.

    The worker namespace is generated using `generate_worker_namespace` if the worker is a string.
    """
    worker = (
        generate_worker_namespace(worker) if isinstance(worker, str) else worker
    )
    task = str(get_current_task().id)
    return str(uuid3(namespace=worker, name=task))


def generate_thread_token(worker: UUID | str) -> str:
    """Generate a thread token using UUIDv3 with the worker namespace and the current thread ID.

    The worker namespace is generated using `generate_worker_namespace` if the worker is a string.
    """
    worker = (
        generate_worker_namespace(worker) if isinstance(worker, str) else worker
    )
    thread = str(get_ident())
    return str(uuid3(namespace=worker, name=thread))
