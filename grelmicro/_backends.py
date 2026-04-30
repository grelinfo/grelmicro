"""Backend Registry.

A generic registry for backend instances. Each module (sync, cache)
maintains its own registry instance with a typed key.
"""

import warnings
from typing import Generic, TypeVar

from grelmicro.errors import GrelmicroError

T = TypeVar("T")


class BackendRegistry(Generic[T]):
    """Generic backend registry.

    Stores a single default backend instance per named slot.
    Registration is explicit: callers invoke
    :meth:`register` (typically from a module-level
    ``use_backend`` helper or a backend's ``__aenter__``).
    Consumers look up the default via :meth:`get`.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the registry.

        Args:
            name: Human-readable name for error messages
                  (e.g. "lock", "cache").
        """
        self._name = name
        self._backend: T | None = None

    def register(self, backend: T) -> None:
        """Register a backend as the default.

        Warns if a different backend is already registered.
        Re-registering the same instance is a no-op.
        """
        if self._backend is backend:
            return
        if self._backend is not None:
            warnings.warn(
                f"Overwriting already-registered {self._name} backend.",
                stacklevel=2,
            )
        self._backend = backend

    def unregister(self, backend: T) -> None:
        """Unregister ``backend`` if it is the current default.

        Identity check: clears the slot only when
        ``self._backend is backend``. Calling on a non-current
        instance is a no-op.
        """
        if self._backend is backend:
            self._backend = None

    def get(self) -> T:
        """Return the registered backend.

        Raises:
            BackendNotLoadedError: If no backend has been registered.
        """
        if self._backend is None:
            msg = (
                f"No {self._name} backend loaded. "
                f"Initialize a backend first "
                f"(e.g. with ``async with`` a backend instance)."
            )
            raise BackendNotLoadedError(msg)
        return self._backend

    @property
    def is_loaded(self) -> bool:
        """Return True if a backend is registered."""
        return self._backend is not None

    def reset(self) -> None:
        """Remove the registered backend unconditionally.

        Intended for test fixtures. Production code should call
        :meth:`unregister` with the instance to clear.
        """
        self._backend = None


class BackendNotLoadedError(GrelmicroError, RuntimeError):
    """Raised when a backend is accessed before being registered."""
