from threading import get_ident
from uuid import NAMESPACE_DNS, UUID, uuid3

from anyio import get_current_task


def generate_worker_namespace(worker: str) -> UUID:
    """Generate a worker UUIDv3 namespace.

    Generate a worker UUID using UUIDv3 with the DNS namespace.
    """
    return uuid3(namespace=NAMESPACE_DNS, name=worker)


def generate_task_token(worker: UUID | str) -> str:
    """Generate a task UUID.

    The worker namespace is generated using `generate_worker_uuid` if the worker is a string.
    Generate a task UUID using UUIDv3 with the worker namespace and the async task ID.
    """
    worker = (
        generate_worker_namespace(worker) if isinstance(worker, str) else worker
    )
    task = str(get_current_task().id)
    return str(uuid3(namespace=worker, name=task))


def generate_thread_token(worker: UUID | str) -> str:
    """Generate a thread UUID.

    The worker namespace is generated using `generate_worker_uuid` if the worker is a string.
    Generate a thread UUID using UUIDv3 with the worker namespace and the current thread ID.
    """
    worker = (
        generate_worker_namespace(worker) if isinstance(worker, str) else worker
    )
    thread = str(get_ident())
    return str(uuid3(namespace=worker, name=thread))
