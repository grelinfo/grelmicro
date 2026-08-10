"""Components for the Grelmicro app object: `RateLimiterComponent`, `CircuitBreakerComponent`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Self, cast

from typing_extensions import Doc

from grelmicro._component import instantiate_if_class
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.resilience._protocol import (
        CircuitBreakerBackend,
        RateLimiterBackend,
    )


class RateLimiterComponent:
    """`RateLimiterBackend` wrapper exposing `(ratelimiter, name)` registration.

    The active app resolves `RateLimiter` patterns to this Component's backend
    on every call.

    Accepts a `Provider` or a `RateLimiterBackend`. When given a Provider, the
    component calls `provider.ratelimiter()` to build the matching adapter.

    Naming this class is rarely needed. `Grelmicro(uses=[redis])` already
    registers a default one for every kind the Provider serves, and a bare
    backend in `uses=[...]` is wrapped for you. Construct it to register a
    second instance under its own name, or to swap the backend in a test with
    `micro.override(...)`.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.providers.redis import RedisProvider
        from grelmicro.resilience import RateLimiter, RateLimiterComponent

        redis = RedisProvider("redis://localhost:6379/0")
        quotas = RedisProvider("redis://localhost:6379/1")
        micro = Grelmicro(uses=[redis, RateLimiterComponent(quotas, name="api")])
        api = RateLimiter.token_bucket(
            "api", capacity=10, refill_rate=1, backend="api"
        )

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
                Registration name. Multiple `RateLimiterComponent` instances
                may coexist on one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the Component with the wrapped backend."""
        self._name = name
        resolved = cast(
            "Provider | RateLimiterBackend",
            instantiate_if_class(source),
        )
        if isinstance(resolved, Provider):
            self._backend = resolved.ratelimiter()
        else:
            self._backend = resolved

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

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
        await self._backend.__aexit__(exc_type, exc, tb)
        return None


class CircuitBreakerComponent:
    """`CircuitBreakerBackend` wrapper exposing `(circuitbreaker, name)` registration.

    The active app resolves `CircuitBreaker` patterns to this Component's
    backend on every call.

    Accepts a `Provider` or a `CircuitBreakerBackend`. When given a Provider,
    the component calls `provider.circuitbreaker()` to build the matching adapter.

    Naming this class is rarely needed. `Grelmicro(uses=[redis])` already
    registers a default one for every kind the Provider serves, and a bare
    backend in `uses=[...]` is wrapped for you. Construct it to keep breaker
    state on a different backend than the rest of the app, to register a second
    instance under its own name, or to swap the backend in a test with
    `micro.override(...)`.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.providers.redis import RedisProvider
        from grelmicro.resilience import (
            CircuitBreaker,
            CircuitBreakerComponent,
            MemoryCircuitBreakerAdapter,
        )

        redis = RedisProvider("redis://localhost:6379/0")
        micro = Grelmicro(
            uses=[redis, CircuitBreakerComponent(MemoryCircuitBreakerAdapter())]
        )
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
                Provider is given, the component calls `provider.circuitbreaker()`
                to build the matching adapter.
                """,
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `CircuitBreakerComponent`
                instances may coexist on one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the Component with the wrapped backend."""
        self._name = name
        resolved = cast(
            "Provider | CircuitBreakerBackend",
            instantiate_if_class(source),
        )
        if isinstance(resolved, Provider):
            self._backend = resolved.circuitbreaker()
        else:
            self._backend = resolved

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

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
        await self._backend.__aexit__(exc_type, exc, tb)
        return None
