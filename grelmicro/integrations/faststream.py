"""FastStream integration that opens a Grelmicro app and binds it per message."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from faststream import BaseMiddleware
from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from faststream import FastStream
    from faststream.message import StreamMessage

    from grelmicro import Grelmicro

    AsyncFuncAny = Callable[[Any], Awaitable[Any]]

__all__ = ["install"]


class _GrelmicroBrokerMiddleware(BaseMiddleware):
    """Bind the active `Grelmicro` app around each consumed message.

    `micro` is read from the `micro` class attribute, set on a dynamically
    created subclass so FastStream can instantiate the middleware per message
    with its own `(msg, *, context)` constructor.
    """

    micro: Grelmicro

    async def consume_scope(
        self, call_next: AsyncFuncAny, msg: StreamMessage[Any]
    ) -> Any:  # noqa: ANN401
        """Bind the app for the handler, reset the binding after it returns."""
        token = self.micro._bind_current()  # noqa: SLF001
        try:
            return await call_next(msg)
        finally:
            self.micro._reset_current(token)  # noqa: SLF001


def install(
    app: Annotated[
        FastStream,
        Doc("The FastStream application to wire."),
    ],
    micro: Annotated[
        Grelmicro,
        Doc("The `Grelmicro` app to open on startup and bind per message."),
    ],
    *,
    ambient: Annotated[
        bool,
        Doc(
            "Register a broker middleware so patterns resolve ambiently inside "
            "subscriber handlers. Default `True`. Pass `False` to skip it."
        ),
    ] = True,
) -> None:
    """Wire `micro` into a FastStream app.

    Opens `async with micro:` on app startup and closes it after shutdown, so
    the components are registered before any message is handled. When `ambient`
    is `True`, registers a broker middleware so patterns resolve through
    `Grelmicro.current()` inside subscriber handlers.

    Prefer the polymorphic `micro.install(app)`, which detects the framework
    and calls this for you.
    """

    @app.on_startup
    async def _open_micro() -> None:
        await micro.__aenter__()

    @app.after_shutdown
    async def _close_micro() -> None:
        await micro.__aexit__(None, None, None)

    if ambient:
        middleware = type(
            "GrelmicroBrokerMiddleware",
            (_GrelmicroBrokerMiddleware,),
            {"micro": micro},
        )
        app.broker.add_middleware(middleware)  # ty: ignore[unresolved-attribute]
    else:
        micro._on_ambient_disabled()  # noqa: SLF001
