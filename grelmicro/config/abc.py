"""Config Source Abstract Base Classes and Protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType


@runtime_checkable
class ConfigBackend(Protocol):
    """Config Backend Protocol.

    A config backend is the pluggable source `ExternalConfig` reads. It
    returns a flat mapping of environment-style keys to string values,
    the same `GREL_...` keys components resolve from the environment. A
    mounted ConfigMap directory, a single `.env` file, an HTTP config
    server, or a git repository each implement this one protocol, so any
    of them can drive live reconfiguration.

    The backend tracks what it last returned so it can report "nothing
    changed" cheaply: `load` returns `None` when the source is unchanged
    since the previous call.
    """

    async def __aenter__(self) -> Self:
        """Open the config backend."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the config backend."""
        ...

    async def load(self) -> Mapping[str, str] | None:
        """Read the current configuration from the source.

        Returns a flat mapping of environment-style keys to string values on
        the first call and whenever the source changes. Returns `None` when
        nothing changed since the last call, so the caller can skip
        re-applying.

        Error contract:

        - Raise `OSError` (or a subclass) when the source is unreadable: a
          missing mount, a permission error, or a network failure.
        - Raise `ValueError` when the source is readable but its content is
          not a valid flat mapping (for example malformed JSON).

        `ExternalConfig` catches both, logs a warning, and keeps the last
        good config, so one bad read never crashes the app or stops future
        polls.
        """
        ...
