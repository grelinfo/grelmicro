"""Problem details component."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from typing_extensions import Doc

if TYPE_CHECKING:
    from types import TracebackType

__all__ = ["ProblemDetails"]


class ProblemDetails:
    """Answer every grelmicro rejection with an RFC 9457 problem detail.

    Register it to opt in, and `micro.install(app)` wires the exception
    handler into FastAPI, Starlette, or Litestar. Without it grelmicro
    registers nothing, and a rejection reaches the framework's own error
    handling exactly as any other exception does.

    A rate limiter over budget, a full bulkhead, an open circuit breaker,
    a lock held elsewhere, or an elapsed deadline then answers the client
    with an `application/problem+json` body carrying what it can act on,
    instead of becoming a `500`.

    Example:
        ```python
        from fastapi import FastAPI

        from grelmicro import Grelmicro
        from grelmicro.http import ProblemDetails

        micro = Grelmicro(uses=[ProblemDetails()])
        app = FastAPI()
        micro.install(app)
        ```

    A framework that serves no HTTP, such as FastStream, ignores it.

    Read more in the [Problem Details](../http/problems.md) docs.
    """

    kind: ClassVar[str] = "problem_details"
    singleton: ClassVar[bool] = True

    def __init__(
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. One rendering answers for the whole app,
                so only one may be registered.
                """
            ),
        ] = "default",
    ) -> None:
        """Initialize the component."""
        self._name = name

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    async def __aenter__(self) -> Self:
        """Open the component.

        Nothing to open. The wiring happens in `micro.install(app)`, which
        reads the registration and adds the handler to the framework before
        it serves. This is the declaration that it should.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the component. Nothing to close."""
        return None
