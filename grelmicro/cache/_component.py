"""Cache component for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from typing_extensions import Doc

from grelmicro._component import instantiate_if_class
from grelmicro.cache.cached import cached
from grelmicro.cache.ttl import TTLCache
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.cache._protocol import CacheBackend
    from grelmicro.cache.serializers import CacheSerializer


class Cache:
    """Cache component: wraps a `CacheBackend` and exposes the `TTLCache` factory.

    Registered as `micro.cache` after `Grelmicro.use(Cache(...))`. The
    `ttl(...)` factory builds a `TTLCache` bound to this component's backend so
    users do not need to thread `backend=` on every cache instance.

    Accepts a `Provider` or a `CacheBackend`. When given a Provider, the
    component calls `provider.cache()` to build the matching adapter.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.cache import Cache, JsonSerializer
        from grelmicro.providers.redis import RedisProvider

        redis = RedisProvider("redis://localhost:6379/0")
        micro = Grelmicro(uses=[redis, Cache(redis)])
        user_cache = micro.cache.ttl(ttl=300, serializer=JsonSerializer())

        @micro.cache.cached(user_cache)
        async def get_user(user_id: int) -> dict:
            ...

        async with micro:
            user = await get_user(1)
        ```

    Read more in the [Cache](../cache.md) docs.
    """

    cached = staticmethod(cached)
    """Re-export of `grelmicro.cache.cached.cached` for app-style ergonomics."""

    kind: ClassVar[str] = "cache"

    def __init__(
        self,
        source: Annotated[
            Provider | CacheBackend | type[Provider | CacheBackend],
            Doc(
                """
                A `Provider` (e.g. `RedisProvider`) or a `CacheBackend`
                instance. When a Provider is given, the component calls
                `provider.cache()` to build the matching adapter. A zero-arg
                class (e.g. `MemoryCacheAdapter`) is instantiated for you.
                """,
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Cache` components may coexist on
                one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the component with the wrapped backend."""
        self._name = name
        source = instantiate_if_class(source)
        if isinstance(source, Provider):
            self._backend = source.cache()
        else:
            self._backend = source

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def backend(self) -> CacheBackend:
        """The underlying `CacheBackend`."""
        return self._backend

    def ttl[T](
        self,
        *,
        ttl: float = 60,
        maxsize: int = 0,
        serializer: CacheSerializer[T] | None = None,
    ) -> TTLCache[T]:
        """Construct a `TTLCache` bound to this component's backend.

        The return type tracks the serializer's type parameter, so passing
        `JsonSerializer[User]()` yields a `TTLCache[User]`.

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
