"""Components for the Grelmicro app object: `RateLimiters`, `CircuitBreakers`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from typing_extensions import Doc

from grelmicro._component import instantiate_if_class
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.resilience._protocol import (
        CircuitBreakerBackend,
        RateLimiterBackend,
    )


class RateLimiters:
    """`RateLimiterBackend` wrapper exposing `(ratelimiter, name)` registration.

    Registered on a `Grelmicro` app via `Grelmicro(uses=[RateLimiters(redis)])`.
    The active app resolves `RateLimiter` patterns to this Component's backend
    on every call.

    Accepts a `Provider` or a `RateLimiterBackend`. When given a Provider, the
    component calls `provider.ratelimiter()` to build the matching adapter.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.providers.redis import RedisProvider
        from grelmicro.resilience import RateLimiter, RateLimiters

        redis = RedisProvider("redis://localhost:6379/0")
        micro = Grelmicro(uses=[redis, RateLimiters(redis)])
        api = RateLimiter.token_bucket("api", capacity=10, refill_rate=1)

        async with micro:
            await api.acquire(key="user-1")
        ```
    """

    kind: ClassVar[str] = "ratelimiter"

    def __init__(
        self,
        source: Annotated[
            Provider | RateLimiterBackend | type[Provider | RateLimiterBackend],
            Doc(
                """
                A `Provider` (e.g. `RedisProvider`) or a `RateLimiterBackend`
                instance. When a Provider is given, the component calls
                `provider.ratelimiter()` to build the matching adapter.
                """,
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `RateLimiters` Components may coexist
                on one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the Component with the wrapped backend."""
        self.name = name
        source = instantiate_if_class(source)
        if isinstance(source, Provider):
            self._backend = source.ratelimiter()
        else:
            self._backend = source

    @property
    def backend(self) -> RateLimiterBackend:
        """The underlying `RateLimiterBackend`."""
        return self._backend

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


class CircuitBreakers:
    """`CircuitBreakerBackend` wrapper exposing `(circuitbreaker, name)` registration.

    Registered on a `Grelmicro` app via `Grelmicro(uses=[CircuitBreakers(redis)])`.
    The active app resolves `CircuitBreaker` patterns to this Component's
    backend on every call.

    Accepts a `Provider` or a `CircuitBreakerBackend`. When given a Provider,
    the component calls `provider.breaker()` to build the matching adapter.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.providers.redis import RedisProvider
        from grelmicro.resilience import CircuitBreaker, CircuitBreakers

        redis = RedisProvider("redis://localhost:6379/0")
        micro = Grelmicro(uses=[redis, CircuitBreakers(redis)])
        payment = CircuitBreaker("payment")

        async with micro:
            async with payment:
                ...
        ```
    """

    kind: ClassVar[str] = "circuitbreaker"

    def __init__(
        self,
        source: Annotated[
            Provider
            | CircuitBreakerBackend
            | type[Provider | CircuitBreakerBackend],
            Doc(
                """
                A `Provider` or a `CircuitBreakerBackend` instance. When a
                Provider is given, the component calls `provider.breaker()` to
                build the matching adapter.
                """,
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `CircuitBreakers` Components may coexist
                on one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the Component with the wrapped backend."""
        self.name = name
        source = instantiate_if_class(source)
        if isinstance(source, Provider):
            self._backend = source.breaker()
        else:
            self._backend = source

    @property
    def backend(self) -> CircuitBreakerBackend:
        """The underlying `CircuitBreakerBackend`."""
        return self._backend

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
