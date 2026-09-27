"""SQLite Provider."""

from __future__ import annotations

import asyncio
import sqlite3
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

import aiosqlite
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Doc

from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

    from grelmicro.cache.sqlite import SQLiteCacheAdapter
    from grelmicro.coordination.sqlite import (
        SQLiteLockAdapter,
        SQLiteReadWriteLockAdapter,
        SQLiteScheduleAdapter,
    )
    from grelmicro.resilience.circuitbreaker.sqlite import (
        SQLiteCircuitBreakerAdapter,
    )
    from grelmicro.resilience.ratelimiter.sqlite import SQLiteRateLimiterAdapter


_BUSY_TIMEOUT_MS = 5000
"""Milliseconds a write waits for another connection holding the file."""

_WAL_SWITCH_TIMEOUT = 5.0
"""Seconds the provider keeps trying to switch the file to WAL."""

_WAL_RETRY_INTERVAL = 0.05
"""Seconds between two attempts at the WAL switch."""

_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
"""Primary result codes that mean another connection holds the file."""


async def _enable_wal(connection: aiosqlite.Connection) -> None:
    """Switch the file to WAL, retrying while another connection holds it.

    SQLite refuses the journal mode switch at once when the file is busy,
    whatever the busy timeout says. The switch is retried every
    `_WAL_RETRY_INTERVAL` until `_WAL_SWITCH_TIMEOUT`. Any other error is
    raised at once.

    Raises:
        sqlite3.OperationalError: When the file stays busy past
            `_WAL_SWITCH_TIMEOUT`, or the switch fails for another reason.
    """
    deadline = monotonic() + _WAL_SWITCH_TIMEOUT
    while True:
        try:
            await connection.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code not in _BUSY_CODES or monotonic() >= deadline:
                raise
            await asyncio.sleep(_WAL_RETRY_INTERVAL)
        else:
            return


class SQLiteConfig(BaseModel):
    """SQLite connection settings.

    Plain `BaseModel` (env-free). Pass to `SQLiteProvider.from_config(cfg)`
    or build a `SQLiteProvider` directly from kwargs. The env path lives
    on the provider, not the config.
    """

    path: str | None = None


class _SQLiteEnvSettings(BaseSettings):
    """Read the SQLite path from the environment (env_prefix-driven)."""

    model_config = SettingsConfigDict(extra="ignore")

    path: str | None = None


def _resolve_path(
    path: str | Path | None,
    env_prefix: str,
    *,
    env_load: bool,
) -> str:
    """Resolve the database path from a kwarg or `{env_prefix}PATH`."""
    if path is not None:
        return str(path)
    if env_load:
        # `_env_prefix` is a pydantic-settings runtime kwarg that overrides
        # `model_config["env_prefix"]` per call. The stubs do not expose it,
        # so static checkers reject it even though the runtime accepts it.
        settings = _SQLiteEnvSettings(_env_prefix=env_prefix)
        if settings.path:
            return settings.path
    msg = f"SQLite path is not set. Pass path=... or set {env_prefix}PATH."
    raise SettingsValidationError(msg)


class SQLiteProvider(Provider):
    """SQLite connection provider.

    Holds the database path and an `aiosqlite` connection opened in
    autocommit mode with WAL journaling. Adapters
    (`SQLiteRateLimiterAdapter`, ...) borrow the connection and a shared
    lock from the provider instead of opening their own, so multiple
    components against the same file share one connection.

    This is the front door for SQLite. Pass it to a Component:

    ```python
    from grelmicro import Grelmicro
    from grelmicro.providers.sqlite import SQLiteProvider
    from grelmicro.resilience import RateLimiterComponent

    sqlite = SQLiteProvider("app.db")
    micro = Grelmicro(uses=[sqlite, RateLimiterComponent(sqlite)])
    ```

    The path can also come from the `SQLITE_PATH` environment variable.
    """

    short_name: ClassVar[str] = "sqlite"

    def __init__(
        self,
        path: Annotated[
            str | Path | None,
            Doc(
                """
                The SQLite database path. When omitted, the path is read
                from `{env_prefix}PATH` (`SQLITE_PATH` by default).
                """
            ),
        ] = None,
        *,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used to resolve the path.
                `SQLITE_PATH` is read out of the box. Override to split
                databases: `CACHE_SQLITE_`, `LOCK_SQLITE_`.
                """
            ),
        ] = "SQLITE_",
        env_load: Annotated[
            bool,
            Doc(
                """
                When True (default), a missing `path` falls back to the
                environment. Set to False to use kwargs only.
                """
            ),
        ] = True,
    ) -> None:
        """Initialize the provider and resolve the database path."""
        self._env_prefix = env_prefix
        self._path = _resolve_path(path, env_prefix, env_load=env_load)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._own = True

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            SQLiteConfig,
            Doc("Pre-built `SQLiteConfig` carrying the database path."),
        ],
        *,
        env_prefix: str = "SQLITE_",
    ) -> Self:
        """Build a provider from a `SQLiteConfig` instance (no env reads)."""
        return cls(path=config.path, env_prefix=env_prefix, env_load=False)

    @classmethod
    def from_client(
        cls,
        client: Annotated[
            aiosqlite.Connection,
            Doc("A pre-built `aiosqlite.Connection` in autocommit mode."),
        ],
        *,
        own: Annotated[
            bool,
            Doc(
                """
                When True, the provider closes the connection on
                `__aexit__`. When False (default), the caller keeps
                ownership.
                """
            ),
        ] = False,
    ) -> Self:
        """Build a provider that wraps an existing `aiosqlite` connection."""
        self = cls.__new__(cls)
        self._env_prefix = "SQLITE_"
        self._path = ""
        self._conn = client
        self._lock = asyncio.Lock()
        self._own = own
        return self

    @property
    def path(self) -> str:
        """Resolved database path (empty for `from_client` providers)."""
        return self._path

    @property
    def env_prefix(self) -> str:
        """Environment variable prefix used to resolve the path."""
        return self._env_prefix

    def __repr__(self) -> str:
        """Return a representation carrying the path."""
        return f"{type(self).__name__}(path={self._path!r})"

    @property
    def client(self) -> aiosqlite.Connection:
        """The underlying `aiosqlite.Connection`.

        Raises:
            OutOfContextError: When accessed before `__aenter__`, or while
                `micro.fake()` leaves this Provider closed.
        """
        if self._conn is None:
            self._refuse_if_left_closed()
            raise OutOfContextError(self, "client")
        return self._conn

    @property
    def connection_lock(self) -> asyncio.Lock:
        """Shared lock serializing access to the single connection."""
        return self._lock

    def ratelimiter(self, **kwargs: Any) -> SQLiteRateLimiterAdapter:  # noqa: ANN401
        """Build a `SQLiteRateLimiterAdapter` bound to this provider."""
        from grelmicro.resilience.ratelimiter.sqlite import (  # noqa: PLC0415
            SQLiteRateLimiterAdapter,
        )

        return SQLiteRateLimiterAdapter(provider=self, **kwargs)

    def lock(self, **kwargs: Any) -> SQLiteLockAdapter:  # noqa: ANN401
        """Build a `SQLiteLockAdapter` bound to this provider."""
        from grelmicro.coordination.sqlite import (  # noqa: PLC0415
            SQLiteLockAdapter,
        )

        return SQLiteLockAdapter(provider=self, **kwargs)

    def readwritelock(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> SQLiteReadWriteLockAdapter:
        """Build a `SQLiteReadWriteLockAdapter` bound to this provider."""
        from grelmicro.coordination.sqlite import (  # noqa: PLC0415
            SQLiteReadWriteLockAdapter,
        )

        return SQLiteReadWriteLockAdapter(provider=self, **kwargs)

    def schedule(self, **kwargs: Any) -> SQLiteScheduleAdapter:  # noqa: ANN401
        """Build a `SQLiteScheduleAdapter` bound to this provider."""
        from grelmicro.coordination.sqlite import (  # noqa: PLC0415
            SQLiteScheduleAdapter,
        )

        return SQLiteScheduleAdapter(provider=self, **kwargs)

    def cache(self, **kwargs: Any) -> SQLiteCacheAdapter:  # noqa: ANN401
        """Build a `SQLiteCacheAdapter` bound to this provider."""
        from grelmicro.cache.sqlite import (  # noqa: PLC0415
            SQLiteCacheAdapter,
        )

        return SQLiteCacheAdapter(provider=self, **kwargs)

    def circuitbreaker(self, **kwargs: Any) -> SQLiteCircuitBreakerAdapter:  # noqa: ANN401
        """Build a `SQLiteCircuitBreakerAdapter` bound to this provider."""
        from grelmicro.resilience.circuitbreaker.sqlite import (  # noqa: PLC0415
            SQLiteCircuitBreakerAdapter,
        )

        return SQLiteCircuitBreakerAdapter(provider=self, **kwargs)

    def _is_open(self) -> bool:
        """Return whether the provider holds an open connection."""
        return self._conn is not None

    async def check(self) -> None:
        """Run `SELECT 1` to prove the connection is open."""
        async with self._lock, self.client.execute("SELECT 1"):
            pass

    async def __aenter__(self) -> Self:
        """Open the connection when the provider owns it."""
        if self._conn is None:
            conn = await aiosqlite.connect(self._path, isolation_level=None)
            try:
                # The busy timeout comes first, so every later write waits for
                # another process instead of failing at once.
                await conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS};")
                await _enable_wal(conn)
            except BaseException:
                await conn.close()
                raise
            self._conn = conn
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection when the provider owns it."""
        if self._own and self._conn is not None:
            await self._conn.close()
            self._conn = None
