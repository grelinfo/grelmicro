"""Idempotency core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Generic, Self, cast

from typing_extensions import Doc, TypeVar

from grelmicro._config import (
    Reconfigurable,
    env_segment,
    resolve_config,
)
from grelmicro.cache._stampede import (
    AsyncStampedeGuard,
    _has_lock_backend,
    _stampede_lock_name,
)
from grelmicro.cache.ttl import _CACHE_PREFIX, TTLCache
from grelmicro.coordination.lock import Lock
from grelmicro.idempotency.config import IdempotencyConfig
from grelmicro.idempotency.errors import IdempotencyConflictError
from grelmicro.metrics import _emit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

    from grelmicro.cache.serializers import CacheSerializer

T = TypeVar("T", default=Any)

_SENTINEL = object()

# The `\x1f` separator stays out of band (no real key uses it) and, unlike
# `\x00`, is valid in a Postgres text key.
_FINGERPRINT_SUFFIX = "\x1ffp"


class Operation(Generic[T]):
    """Handle for a single idempotent operation, yielded by `Idempotency.__call__`.

    On a replay, `replayed` is True and `response` carries the stored
    value. On a first execution, `replayed` is False and the body runs
    the work and calls `store(response)` to persist it.
    """

    def __init__(self, *, replayed: bool, response: T | None) -> None:
        """Initialize the operation handle."""
        self._replayed = replayed
        self._response = response
        self._stored: T | None = None
        self._has_store = False

    @property
    def replayed(self) -> bool:
        """Whether the key was already stored and the response replayed."""
        return self._replayed

    @property
    def response(self) -> T | None:
        """The stored response when `replayed` is True, else None."""
        return self._response

    def store(
        self,
        response: Annotated[
            T,
            Doc("The response to store and replay on later calls."),
        ],
    ) -> None:
        """Record the response to persist for this key.

        Called once during a first execution. The value is written when
        the block exits without an exception. Calling it on a replay is
        a no-op.
        """
        if self._replayed:
            return
        self._stored = response
        self._has_store = True


class _Block(Generic[T]):
    """Async context manager driving one `Idempotency` key.

    On enter, reads the stored response and replays it when present.
    Otherwise it holds the single-flight lock across the block body so a
    duplicate arriving mid-flight waits and replays the stored response.
    On exit, persists the response when one was stored and no exception
    propagated, then releases the lock.
    """

    def __init__(
        self,
        idempotency: Idempotency[T],
        key: str,
        fingerprint: str | None,
    ) -> None:
        """Initialize the block."""
        self._idempotency = idempotency
        self._key = key
        self._fingerprint = fingerprint
        self._operation: Operation[T] | None = None
        self._local_lock: Any = None
        self._distributed_lock: Lock | None = None

    async def __aenter__(self) -> Operation[T]:
        """Return an `Operation`, replaying or starting a first execution.

        Raises:
            OutOfContextError: No cache backend resolved in this scope.
                Pass `cache=`, register a `Cache` Component, or run the
                call under the app context (for FastAPI, add
                `GrelmicroMiddleware`).
        """
        from grelmicro._app import (  # noqa: PLC0415
            ComponentNotRegisteredError,
            NoActiveAppError,
        )
        from grelmicro.errors import OutOfContextError  # noqa: PLC0415

        try:
            replay = await self._idempotency._replay(  # noqa: SLF001
                self._key, self._fingerprint
            )
        except (
            NoActiveAppError,
            ComponentNotRegisteredError,
            OutOfContextError,
        ):
            msg = (
                f"Idempotency({self._idempotency.name!r}) resolved no "
                f"cache backend. Pass cache=, register a Cache "
                f"component, or run the call under the app context (for "
                f"FastAPI add GrelmicroMiddleware)."
            )
            raise OutOfContextError(msg) from None
        if replay is not _SENTINEL:
            _emit.incr(
                "grelmicro.idempotency.operations",
                **{
                    "idempotency.name": self._idempotency._name,  # noqa: SLF001
                    "result": "replay",
                },
            )
            self._operation = Operation(replayed=True, response=replay)
            return self._operation

        guard = self._idempotency._guard  # noqa: SLF001
        self._local_lock = await guard.get_lock(self._key)
        await self._local_lock.acquire()
        try:
            replay = await self._idempotency._replay(  # noqa: SLF001
                self._key, self._fingerprint
            )
            if replay is not _SENTINEL:
                self._local_lock.release()
                self._local_lock = None
                _emit.incr(
                    "grelmicro.idempotency.operations",
                    **{
                        "idempotency.name": self._idempotency._name,  # noqa: SLF001
                        "result": "replay",
                    },
                )
                self._operation = Operation(replayed=True, response=replay)
                return self._operation

            if _has_lock_backend():
                self._distributed_lock = Lock(
                    _stampede_lock_name(
                        self._idempotency._scoped(self._key)  # noqa: SLF001
                    )
                )
                await self._distributed_lock.acquire()
                replay = await self._idempotency._replay(  # noqa: SLF001
                    self._key, self._fingerprint
                )
                if replay is not _SENTINEL:
                    await self._release()
                    _emit.incr(
                        "grelmicro.idempotency.operations",
                        **{
                            "idempotency.name": self._idempotency._name,  # noqa: SLF001
                            "result": "replay",
                        },
                    )
                    self._operation = Operation(replayed=True, response=replay)
                    return self._operation
        except BaseException:
            await self._release()
            raise

        _emit.incr(
            "grelmicro.idempotency.operations",
            **{
                "idempotency.name": self._idempotency._name,  # noqa: SLF001
                "result": "execute",
            },
        )
        self._operation = Operation(replayed=False, response=None)
        return self._operation

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Persist the stored response on success, then release the lock."""
        operation = self._operation
        try:
            if (
                exc_type is None
                and operation is not None
                and not operation._replayed  # noqa: SLF001
                and operation._has_store  # noqa: SLF001
            ):
                await self._idempotency._persist(  # noqa: SLF001
                    self._key,
                    cast("T", operation._stored),  # noqa: SLF001
                    self._fingerprint,
                )
        finally:
            await self._release()

    async def _release(self) -> None:
        """Release the distributed and in-process locks, in that order."""
        if self._distributed_lock is not None:
            await self._distributed_lock.release()
            self._distributed_lock = None
        if self._local_lock is not None:
            self._local_lock.release()
            self._local_lock = None


class Idempotency(Reconfigurable[IdempotencyConfig], Generic[T]):
    """Idempotency keys for safe retries of an operation.

    Each named `Idempotency` stores a response under a caller-supplied
    key for `ttl` seconds. A repeated key within that window replays the
    stored response without running the operation again. A duplicate
    arriving while the first execution is in flight waits and receives
    the stored response, across replicas when a lock backend is
    configured and in-process otherwise. An exception in the block
    stores nothing and a later retry with the same key executes fresh.

    Storage rides the cache layer. Pass an explicit `cache` to bind a
    `TTLCache`, or leave it unset to resolve the active app's `Cache`
    component. Without either, `OutOfContextError` is raised on first
    use.

    Supports live reconfiguration via `reconfigure(new_config)`. A swap
    takes effect on the next call. In-flight calls keep the config they
    started with.

    The type parameter `T` represents the stored response type. Defaults
    to `Any` when unspecified.
    """

    _IDEMPOTENCY_PREFIX = "idempotency"

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                """
                The idempotency namespace.

                Keys are scoped under this name, so the same key used by
                two different `Idempotency` instances does not collide.
                """,
            ),
        ],
        *,
        ttl: Annotated[
            float | None,
            Doc(
                """
                Lifetime in seconds of a stored response.

                Default: 86400. When unset and env reads are enabled (see
                `env_load` and `GREL_ENV_LOAD`), resolves from the
                environment variable
                `GREL_IDEMPOTENCY_{NAME_UPPER}_TTL` if present, otherwise
                falls back to the `IdempotencyConfig` default.
                """,
            ),
        ] = None,
        fingerprint: Annotated[
            str | None,
            Doc(
                """
                Default payload fingerprint applied to every call.

                A per-call `fingerprint=` on `__call__` overrides this. A
                replay with a fingerprint different from the stored one
                raises `IdempotencyConflictError`. When None, no check.
                """,
            ),
        ] = None,
        cache: Annotated[
            TTLCache[T] | None,
            Doc(
                """
                The `TTLCache` used to store responses.

                By default, a `TTLCache` is composed internally and
                resolves the active app's `Cache` component backend.
                """,
            ),
        ] = None,
        serializer: Annotated[
            CacheSerializer[T] | None,
            Doc(
                """
                Serialization strategy for stored responses.

                Ignored when an explicit `cache` is given. Otherwise
                passed to the internally composed `TTLCache`. Defaults to
                `JsonSerializer`.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: `GREL_IDEMPOTENCY_{NAME_UPPER}_`.
                """,
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                `GREL_ENV_LOAD` flag. Pass True or False to override the
                flag for this construction.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the idempotency namespace."""
        resolved_env_prefix = (
            env_prefix or f"GREL_IDEMPOTENCY_{env_segment(name)}_"
        )
        config = resolve_config(
            IdempotencyConfig,
            explicit=None,
            kwargs={"ttl": ttl},
            env_prefix=resolved_env_prefix,
            env_load=env_load,
        )
        self._setup(name, config, fingerprint, cache, serializer)
        self._track_reconfigure(resolved_env_prefix)

    @classmethod
    def from_config(
        cls,
        name: Annotated[
            str,
            Doc("The idempotency namespace. Acts as the instance identity."),
        ],
        config: Annotated[
            IdempotencyConfig,
            Doc(
                """
                The pre-built idempotency configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree. The environment path is
                bypassed and the config is used as-is.
                """,
            ),
        ],
        *,
        fingerprint: Annotated[
            str | None,
            Doc("Default payload fingerprint applied to every call."),
        ] = None,
        cache: Annotated[
            TTLCache[T] | None,
            Doc("The `TTLCache` used to store responses."),
        ] = None,
        serializer: Annotated[
            CacheSerializer[T] | None,
            Doc("Serialization strategy for stored responses."),
        ] = None,
    ) -> Self:
        """Construct an `Idempotency` from a name and a pre-built config."""
        instance = cls.__new__(cls)
        instance._setup(name, config, fingerprint, cache, serializer)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: IdempotencyConfig,
        fingerprint: str | None,
        cache: TTLCache[T] | None,
        serializer: CacheSerializer[T] | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        import asyncio  # noqa: PLC0415

        from grelmicro.cache.serializers import JsonSerializer  # noqa: PLC0415

        self._name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._fingerprint = fingerprint
        self._guard = AsyncStampedeGuard()
        if cache is not None:
            self._cache: TTLCache[T] = cache
        else:
            resolved_serializer: CacheSerializer[Any] = (
                serializer if serializer is not None else JsonSerializer()
            )
            self._cache = cast(
                "TTLCache[T]",
                TTLCache(ttl=config.ttl, serializer=resolved_serializer),
            )

    @property
    def name(self) -> str:
        """Return the idempotency namespace."""
        return self._name

    def __call__(
        self,
        key: Annotated[
            str,
            Doc("The idempotency key derived from the request."),
        ],
        *,
        fingerprint: Annotated[
            str | None,
            Doc(
                """
                Payload fingerprint for this call.

                Overrides the instance-level `fingerprint`. A replay with
                a different fingerprint raises `IdempotencyConflictError`.
                When None, the instance default applies.
                """,
            ),
        ] = None,
    ) -> _Block[T]:
        """Open an idempotent block for `key`.

        Use as an async context manager. The yielded `Operation` carries
        `replayed`, `response`, and `store(...)`.
        """
        return _Block(
            self,
            key,
            fingerprint if fingerprint is not None else self._fingerprint,
        )

    async def run(
        self,
        key: Annotated[
            str,
            Doc("The idempotency key derived from the request."),
        ],
        factory: Annotated[
            Callable[[], T] | Callable[[], Awaitable[T]],
            Doc(
                "Sync or async callable that produces the response on a"
                " first execution. Awaited when it returns a coroutine."
            ),
        ],
        *,
        fingerprint: Annotated[
            str | None,
            Doc(
                """
                Payload fingerprint for this call.

                Overrides the instance-level `fingerprint`. A replay with
                a different fingerprint raises `IdempotencyConflictError`.
                When None, the instance default applies.
                """,
            ),
        ] = None,
    ) -> T:
        """Run an operation once for `key`, then replay its response.

        On a first execution the `factory` runs, its result is stored,
        and the result is returned. A later call with the same key within
        `ttl` replays the stored response without running the `factory`
        again. A failing `factory` stores nothing, so a later retry runs
        fresh.
        """
        import asyncio  # noqa: PLC0415

        async with self(key, fingerprint=fingerprint) as operation:
            if operation.replayed:
                return cast("T", operation.response)
            response = factory()
            if asyncio.iscoroutine(response):
                response = await response
            operation.store(cast("T", response))
            return cast("T", response)

    def _scoped(self, key: str) -> str:
        """Return the namespace-scoped storage key."""
        return f"{self._IDEMPOTENCY_PREFIX}:{self._name}:{key}"

    async def _replay(self, key: str, fingerprint: str | None) -> Any:  # noqa: ANN401
        """Return the stored response, or the miss sentinel.

        Raises `IdempotencyConflictError` when a fingerprint is given and
        the stored fingerprint differs.
        """
        scoped = self._scoped(key)
        stored = await self._cache.get(scoped, cast("T", _SENTINEL))
        if stored is _SENTINEL:
            return _SENTINEL
        if fingerprint is not None:
            saved = await self._cache._get_backend().get(  # noqa: SLF001
                key=f"{_CACHE_PREFIX}:{scoped}{_FINGERPRINT_SUFFIX}"
            )
            if saved is not None and saved.decode() != fingerprint:
                raise IdempotencyConflictError(name=self._name, key=key)
        return stored

    async def _persist(
        self, key: str, response: T, fingerprint: str | None
    ) -> None:
        """Store the response and the optional fingerprint under `ttl`."""
        scoped = self._scoped(key)
        ttl = self._config.ttl
        await self._cache.set(scoped, response, ttl)
        if fingerprint is not None:
            await self._cache._get_backend().set(  # noqa: SLF001
                key=f"{_CACHE_PREFIX}:{scoped}{_FINGERPRINT_SUFFIX}",
                value=fingerprint.encode(),
                ttl=ttl,
            )
