"""The ExternalConfig component."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack, suppress
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro._config import reconfigure_all
from grelmicro._discovery import load_adapter
from grelmicro.config.file import FileConfigAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping
    from os import PathLike
    from types import TracebackType

    from grelmicro.config.abc import ConfigBackend

logger = logging.getLogger("grelmicro")

_DEFAULT_INTERVAL = 10.0


def _detect_scheme(text: str) -> str:
    """Return the adapter short name for a source string.

    A URL picks the matching network adapter, anything else is a local
    filesystem path.
    """
    low = text.lower()
    if low.startswith(("http://", "https://")):
        return "http"
    if low.startswith(("git@", "git+", "ssh://")) or low.endswith(".git"):
        return "git"
    return "file"


def _coerce_source(value: ConfigBackend | str | PathLike[str]) -> ConfigBackend:
    """Build a `ConfigBackend` from a string, path, or pass one through.

    A `str` or `PathLike` is routed by scheme: an `http(s)://` URL to the
    HTTP adapter, a git URL to the git adapter, anything else to
    `FileConfigAdapter`. The network adapters resolve through the
    `grelmicro.config.adapters` entry points, so they raise
    `AdapterNotRegisteredError` until their extra is installed.

    Raises:
        AdapterNotRegisteredError: The source needs an adapter that is
            not installed (for example a URL without the matching extra).
    """
    if isinstance(value, (str, os.PathLike)):
        text = os.fspath(value)
        scheme = _detect_scheme(text)
        if scheme == "file":
            return FileConfigAdapter(text)
        return load_adapter("config", scheme)(text)
    return value


class ExternalConfig:
    """Reconfigure live components from an external source.

    Implements the Externalized Configuration pattern: configuration lives
    outside the image and is applied at runtime. Add it to a `Grelmicro`
    app and every component that resolves from the environment is kept in
    sync with a mounted ConfigMap, Secret, or any other
    [`ConfigBackend`][grelmicro.config.abc.ConfigBackend], with no
    per-component wiring.

    ```python
    from grelmicro import Grelmicro, ExternalConfig
    from grelmicro.coordination import Coordination, Lock
    from grelmicro.coordination.redis import RedisLockAdapter

    ledger_lock = Lock("ledger")

    micro = Grelmicro(uses=[
        Coordination(RedisLockAdapter()),
        ExternalConfig(
            config="/etc/grelmicro/config",
            secrets="/etc/grelmicro/secrets",
        ),
    ])
    ```

    Config and secrets are separate sources so sensitive values live in a
    Secret and the rest in a ConfigMap, the same split the platform makes.
    Both carry the same `GREL_...` keys components read from the
    environment. On a key collision the secret wins.

    A `str` or path is routed by scheme: a local path uses
    [`FileConfigAdapter`][grelmicro.config.file.FileConfigAdapter], an
    `http(s)://` or git URL uses the matching adapter.

    The source is applied once when the app opens, then polled. Only keys
    present in the source are applied, so a generated lock `worker` and any
    field the source omits keep their value. An invalid value is logged and
    skipped, leaving the running config in place. List the component last in
    `uses=` so the components it reconfigures open first.

    Every named component is addressable by its `GREL_...` prefix
    (`GREL_RATELIMITER_API_*`, `GREL_CIRCUITBREAKER_PAYMENTS_*`, ...),
    whether or not it loaded any value from the environment. A component
    built through its `from_config` classmethod opts out of live reload and
    stays on the config it was constructed with.

    A bad poll never crashes the app or stops future polls: an adapter that
    raises on an unreadable source is logged and the last good config is
    kept. Call [`reload`][grelmicro.config.ExternalConfig.reload] for a
    deterministic load-and-apply pass instead of waiting for the next poll.
    """

    def __init__(
        self,
        config: Annotated[
            ConfigBackend | str | PathLike[str] | None,
            Doc(
                """
                The configuration source: a mounted ConfigMap directory, a
                `.env` or `.json` file, a URL, or a `ConfigBackend`. Holds
                the non-sensitive `GREL_...` keys.
                """,
            ),
        ] = None,
        *,
        secrets: Annotated[
            ConfigBackend | str | PathLike[str] | None,
            Doc(
                """
                The secrets source: a mounted Secret directory or any other
                `ConfigBackend`. Its keys override `config` on collision.
                """,
            ),
        ] = None,
        interval: Annotated[
            float,
            Doc(
                """
                Seconds between polls of the sources. Each poll re-applies
                only what changed.
                """,
            ),
        ] = _DEFAULT_INTERVAL,
    ) -> None:
        """Initialize the external config reloader.

        Raises:
            ValueError: If neither `config` nor `secrets` is given.
        """
        if config is None and secrets is None:
            msg = "ExternalConfig requires a config source, a secrets source, or both"
            raise ValueError(msg)
        self._config_src = (
            _coerce_source(config) if config is not None else None
        )
        self._secrets_src = (
            _coerce_source(secrets) if secrets is not None else None
        )
        self._interval = interval
        self._config_data: Mapping[str, str] | None = None
        self._secrets_data: Mapping[str, str] | None = None
        self._stack: AsyncExitStack | None = None
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        """Open the sources, apply once, then start polling."""
        stack = AsyncExitStack()
        await stack.__aenter__()
        if self._config_src is not None:
            await stack.enter_async_context(self._config_src)  # ty: ignore[invalid-argument-type]
        if self._secrets_src is not None:
            await stack.enter_async_context(self._secrets_src)  # ty: ignore[invalid-argument-type]
        self._stack = stack
        await self.reload()
        self._task = asyncio.create_task(self._poll())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop polling and close the sources."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc_value, traceback)
            self._stack = None

    async def reload(self) -> None:
        """Load the sources once and reconfigure every live component.

        Performs one immediate load-and-apply pass, the same pass the
        poll loop runs on each interval. Use it for a deterministic
        trigger in tests and ops runbooks instead of waiting for the next
        poll.

        An adapter that raises on an unreadable source is logged and the
        last good config is kept, so a single failed reload never raises.
        Values rejected by a component are logged and skipped by
        `reconfigure_all`, leaving the running config in place.
        """
        try:
            merged = await self._load_merged()
        except Exception:  # noqa: BLE001
            logger.warning(
                "External config reload failed, keeping last good config",
                exc_info=True,
            )
            return
        if merged is None:
            return
        await reconfigure_all(merged)

    async def _poll(self) -> None:
        """Re-apply the sources on every interval until cancelled."""
        while True:
            await asyncio.sleep(self._interval)
            await self.reload()

    async def _load_merged(self) -> Mapping[str, str] | None:
        """Return the merged mapping, or `None` when no source has data yet.

        Each source reports `None` when unchanged, so the last seen mapping
        is kept and reused. Secrets override config on a key collision.
        """
        if self._config_src is not None:
            data = await self._config_src.load()
            if data is not None:
                self._config_data = data
        if self._secrets_src is not None:
            data = await self._secrets_src.load()
            if data is not None:
                self._secrets_data = data
        if self._config_data is None and self._secrets_data is None:
            return None
        return {**(self._config_data or {}), **(self._secrets_data or {})}
