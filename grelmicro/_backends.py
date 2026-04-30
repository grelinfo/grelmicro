"""Backend Registry."""

import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Generic, TypeVar

from grelmicro.errors import GrelmicroError

T = TypeVar("T")

DEFAULT_NAME = "default"

# Subscribed at construction time so ``grelmicro.lifespan()`` walks
# only the registries whose modules were actually imported.
_ALL_REGISTRIES: dict[str, "BackendRegistry"] = {}


class BackendRegistry(Generic[T]):
    """Multi-name backend registry with task-scoped overrides."""

    def __init__(self, *, name: str) -> None:
        """Initialize the registry."""
        self._name = name
        self._backends: dict[str, T] = {}
        self._overrides: ContextVar[dict[str, T] | None] = ContextVar(
            f"{name}_backend_overrides", default=None
        )
        _ALL_REGISTRIES[name] = self

    def _current_overrides(self) -> dict[str, T]:
        """Return the current task-scoped overrides (read-only view)."""
        return self._overrides.get() or {}

    def register(self, name: str, backend: T) -> None:
        """Register ``backend`` under ``name``.

        Re-registering the same instance is a no-op. Registering
        a different instance under an existing name warns and
        replaces.
        """
        existing = self._backends.get(name)
        if existing is backend:
            return
        if existing is not None:
            warnings.warn(
                f"Overwriting already-registered {self._name} "
                f"backend {name!r}.",
                stacklevel=2,
            )
        self._backends[name] = backend

    def unregister(self, name: str, backend: T | None = None) -> None:
        """Remove the entry for ``name``.

        When ``backend`` is provided, the slot is cleared only
        if the registered instance is identical.
        """
        existing = self._backends.get(name)
        if existing is None:
            return
        if backend is not None and existing is not backend:
            return
        del self._backends[name]

    def items(self) -> Iterator[tuple[str, T]]:
        """Iterate over registered (name, backend) pairs."""
        return iter(self._backends.items())

    def get(self, name: str = DEFAULT_NAME) -> T:
        """Resolve a backend by ``name``.

        Lookup order:

        1. Task-scoped override for ``name``.
        2. Registered entry under ``name``.
        3. When ``name`` is ``"default"`` and exactly one
           backend is registered: that sole entry.

        Raises:
            BackendNotLoadedError: If nothing resolves.
        """
        overrides = self._current_overrides()
        if name in overrides:
            return overrides[name]
        if name in self._backends:
            return self._backends[name]
        if name == DEFAULT_NAME and len(self._backends) == 1:
            return next(iter(self._backends.values()))
        if name == DEFAULT_NAME and len(self._backends) > 1:
            registered = sorted(self._backends)
            msg = (
                f"No default {self._name} backend: multiple are "
                f"registered ({registered}), none named "
                f"{DEFAULT_NAME!r}."
            )
            raise BackendNotLoadedError(msg)
        msg = f"No {self._name} backend loaded for name {name!r}."
        raise BackendNotLoadedError(msg)

    @property
    def is_loaded(self) -> bool:
        """Return True if any backend is registered."""
        return bool(self._backends)

    def reset(self) -> None:
        """Clear every registered backend."""
        self._backends.clear()

    @contextmanager
    def use(
        self,
        backend: T | None = None,
        /,
        **named: T,
    ) -> Iterator[None]:
        """Install task-scoped overrides for the duration of the block.

        ``use(backend)`` overrides the ``"default"`` slot.
        ``use(default=a, analytics=b)`` overrides multiple names.
        Inner blocks shadow outer ones for the names they specify.
        """
        overlay: dict[str, T] = dict(self._current_overrides())
        if backend is not None:
            overlay[DEFAULT_NAME] = backend
        overlay.update(named)
        token = self._overrides.set(overlay)
        try:
            yield
        finally:
            self._overrides.reset(token)


class BackendNotLoadedError(GrelmicroError, RuntimeError):
    """Raised when a backend is accessed before being registered."""
