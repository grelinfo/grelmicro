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
    Backends register themselves on initialization (via
    ``auto_register=True``) and consumers look up the default
    via ``get()``.
    """

    def __init__(self, *, name: str) -> None:
        """Initialize the registry.

        Args:
            name: Human-readable name for error messages
                  (e.g. "lock", "cache").
        """
        self._name = name
        self._backend: T | None = None

    def set(self, backend: T) -> None:
        """Register a backend as the default."""
        if self._backend is not None:
            warnings.warn(
                f"Overwriting already-registered {self._name} backend.",
                stacklevel=2,
            )
        self._backend = backend

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
        """Remove the registered backend."""
        self._backend = None


class BackendNotLoadedError(GrelmicroError, RuntimeError):
    """Raised when a backend is accessed before being registered."""
