"""Memory Backend.

This backend is used for testing purposes and should not be used in production. Memory backends are
not persistent and are not shared between different workers.
"""

from grelmicro.backends.memory.lock import MemoryLockBackend

__all__ = ["MemoryLockBackend"]
