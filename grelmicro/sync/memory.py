"""Memory Synchronization Backend."""

from time import monotonic
from types import TracebackType
from typing import Annotated, Self

from typing_extensions import Doc

from grelmicro.sync._backends import loaded_backends
from grelmicro.sync.abc import SyncBackend


class MemorySyncBackend(SyncBackend):
    """Memory Synchronization Backend.

    This is not a backend with a real distributed lock. It is a local lock that can be used for
    testing purposes or for locking operations that are executed in the same AnyIO event loop.
    """

    def __init__(
        self,
        *,
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register the lock backend in the backend registry."
            ),
        ] = True,
    ) -> None:
        """Initialize the lock backend."""
        self._locks: dict[str, tuple[str | None, float]] = {}
        if auto_register:
            loaded_backends["lock"] = self

    async def __aenter__(self) -> Self:
        """Enter the lock backend."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the lock backend."""
        self._locks.clear()

    async def acquire(self, *, name: str, token: str, duration: float) -> bool:
        """Acquire the lock."""
        current_token, expire_at = self._locks.get(name, (None, 0))
        if (
            current_token is None
            or current_token == token
            or expire_at < monotonic()
        ):
            self._locks[name] = (token, monotonic() + duration)
            return True
        return False

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        current_token, expire_at = self._locks.get(name, (None, 0))
        if current_token == token and expire_at >= monotonic():
            del self._locks[name]
            return True
        if current_token and expire_at < monotonic():
            del self._locks[name]
        return False

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        current_token, expire_at = self._locks.get(name, (None, 0))
        return current_token is not None and expire_at >= monotonic()

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        current_token, expire_at = self._locks.get(name, (None, 0))
        return current_token == token and expire_at >= monotonic()
