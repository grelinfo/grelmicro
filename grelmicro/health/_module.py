"""Health module for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.health._registry import HealthRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from grelmicro.health._models import HealthReport
    from grelmicro.health._types import HealthCheckFunc


class Health:
    """Health module: wraps a `HealthRegistry` for the `Grelmicro` app.

    Registered as `micro.health` after `Grelmicro.use(Health())`. Forwards
    `check(...)` to the underlying registry so the user can decorate health
    checks directly on the module:

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.health import Health

        micro = Grelmicro(modules=[Health()])

        @micro.health.check("redis")
        async def redis_alive() -> None:
            ...

        async with micro:
            report = await micro.health.run()
        ```

    Read more in the [Health](../health.md) docs.
    """

    kind: ClassVar[str] = "health"

    def __init__(
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Health` modules may coexist on
                one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
        registry: Annotated[
            HealthRegistry | None,
            Doc(
                """
                A pre-built `HealthRegistry`. When `None`, a fresh default one
                is constructed.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the module with the wrapped registry."""
        self.name = name
        self._registry = registry or HealthRegistry()

    @property
    def registry(self) -> HealthRegistry:
        """The underlying `HealthRegistry`."""
        return self._registry

    def check(
        self,
        name: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> Callable[[HealthCheckFunc], HealthCheckFunc]:
        """Decorate an async function to register it as a health check.

        Forwards to `HealthRegistry.check`. See the underlying method for
        keyword arguments such as `critical=` and `timeout=`.
        """
        return self._registry.check(name, **kwargs)

    async def run(self, **kwargs: Any) -> HealthReport:  # noqa: ANN401
        """Run the registered checks and return the aggregate `HealthReport`.

        Forwards to `HealthRegistry.run`. See the underlying method for keyword
        arguments such as `critical_only=` and `exclude=`.
        """
        return await self._registry.run(**kwargs)

    async def __aenter__(self) -> Self:
        """Open the underlying registry."""
        await self._registry.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the underlying registry."""
        return await self._registry.__aexit__(exc_type, exc, tb)
