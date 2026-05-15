"""Components for the Grelmicro app object: `RateLimit`, `Breaker`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from typing_extensions import Doc

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.resilience._protocol import (
        CircuitBreakerBackend,
        RateLimiterBackend,
    )


class RateLimit:
    """`RateLimiterBackend` wrapper exposing `(ratelimiter, name)` registration.

    Registered on a `Grelmicro` app via `Grelmicro(uses=[RateLimit(adapter)])`.
    The active app resolves `RateLimiter` patterns to this Component's backend
    on every call.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.resilience import RateLimit, RateLimiter
        from grelmicro.resilience.redis import RedisRateLimiterAdapter

        micro = Grelmicro(uses=[RateLimit(RedisRateLimiterAdapter())])
        api = RateLimiter.token_bucket("api", capacity=10, refill_rate=1)

        async with micro:
            await api.acquire(key="user-1")
        ```
    """

    kind: ClassVar[str] = "ratelimiter"

    def __init__(
        self,
        backend: Annotated[
            RateLimiterBackend,
            Doc("The rate limiter backend opened with the Component."),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `RateLimit` Components may coexist
                on one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the Component with the wrapped backend."""
        self.name = name
        self._backend = backend

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


class Breaker:
    """`CircuitBreakerBackend` wrapper exposing `(circuitbreaker, name)` registration.

    Registered on a `Grelmicro` app via `Grelmicro(uses=[Breaker(adapter)])`.
    The active app resolves `CircuitBreaker` patterns to this Component's
    backend on every call.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.resilience import Breaker, CircuitBreaker
        from grelmicro.resilience.memory import MemoryCircuitBreakerAdapter

        micro = Grelmicro(uses=[Breaker(MemoryCircuitBreakerAdapter())])
        payment = CircuitBreaker("payment")

        async with micro:
            async with payment:
                ...
        ```
    """

    kind: ClassVar[str] = "circuitbreaker"

    def __init__(
        self,
        backend: Annotated[
            CircuitBreakerBackend,
            Doc("The circuit breaker backend opened with the Component."),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Breaker` Components may coexist
                on one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the Component with the wrapped backend."""
        self.name = name
        self._backend = backend

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
