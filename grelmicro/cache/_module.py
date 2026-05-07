"""Cache module for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.cache.ttl import TTLCache

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.cache._protocol import CacheBackend
    from grelmicro.cache.serializers import CacheSerializer


class Cache:
    """Cache module: wraps a `CacheBackend` and exposes the `TTLCache` factory.

    Registered as `micro.cache` after `Grelmicro.use(Cache(backend))`. The
    `ttl(...)` factory builds a `TTLCache` bound to this module's backend so
    users do not need to thread `backend=` on every cache instance.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.cache import Cache, JsonSerializer
        from grelmicro.cache.redis import RedisCacheBackend

        micro = Grelmicro(modules=[Cache(RedisCacheBackend("redis://localhost"))])

        async with micro:
            user_cache = micro.cache.ttl(ttl=300, serializer=JsonSerializer())
            await user_cache.set("alice", {"id": 1})
        ```

    Read more in the [Cache](../cache.md) docs.
    """

    kind: ClassVar[str] = "cache"

    def __init__(
        self,
        backend: Annotated[
            CacheBackend,
            Doc("The cache backend opened with the module."),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Cache` modules may coexist on
                one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the module with the wrapped backend."""
        self.name = name
        self._backend = backend

    @property
    def backend(self) -> CacheBackend:
        """The underlying `CacheBackend`."""
        return self._backend

    def ttl(
        self,
        *,
        ttl: float = 60,
        maxsize: int = 0,
        serializer: CacheSerializer[Any] | None = None,
    ) -> TTLCache[Any]:
        """Construct a `TTLCache` bound to this module's backend.

        Args:
            ttl: Default TTL in seconds for cached entries.
            maxsize: Maximum local cache entries (`0` means unlimited).
            serializer: Serialization strategy. Defaults to raw bytes.
        """
        return TTLCache(
            maxsize=maxsize,
            ttl=ttl,
            backend=self._backend,
            serializer=serializer,
        )

    async def __aenter__(self) -> Self:
        """Open the underlying backend."""
        await self._backend.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the underlying backend."""
        return await self._backend.__aexit__(exc_type, exc, tb)
