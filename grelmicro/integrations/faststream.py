"""FastStream integration that opens a Grelmicro app and binds it per message."""

from __future__ import annotations

import importlib
import logging
import sys
from typing import TYPE_CHECKING, Annotated, Any, cast

from faststream import BaseMiddleware
from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from faststream import FastStream
    from faststream.message import StreamMessage

    from grelmicro import Grelmicro
    from grelmicro.trace._component import Trace

    AsyncFuncAny = Callable[[Any], Awaitable[Any]]

__all__ = ["install"]

_logger = logging.getLogger(__name__)


def _instrument_broker(app: FastStream, micro: Grelmicro) -> None:
    """Add the broker's OpenTelemetry telemetry middleware per `Trace(instrument=...)`.

    Runs at install time, before the broker starts, so message spans are wired
    before the first message is consumed. The middleware is created with no
    explicit `TracerProvider`, so OTel's global resolves to the provider `Trace`
    installs during the lifespan, exactly like the FastAPI request-span path. It
    is a no-op without faststream's telemetry support for the broker, or when
    `faststream` is not selected by the directive.
    """
    from grelmicro.trace._autoinstrument import (  # noqa: PLC0415
        explicit_names,
        is_selected,
    )

    component = next(
        (c for c in micro.components if getattr(c, "kind", None) == "trace"),
        None,
    )
    if component is None:
        return
    trace = cast("Trace", component)
    directive = trace.instrument
    if not is_selected("faststream", directive):
        return
    broker = getattr(app, "broker", None)
    if broker is None:  # pragma: no cover
        return
    # Broker family from the module path, e.g. faststream.redis.broker -> redis.
    family = type(broker).__module__.split(".")[1]
    try:
        module = importlib.import_module(f"faststream.{family}.opentelemetry")
    except ImportError:
        names = explicit_names(directive)
        if names is not None and "faststream" in names:
            _logger.warning(
                "Trace named 'faststream' for instrumentation but "
                "faststream.%s.opentelemetry is not available.",
                family,
            )
        return
    middleware_cls = next(
        (
            getattr(module, name)
            for name in dir(module)
            if name.endswith("TelemetryMiddleware")
        ),
        None,
    )
    if middleware_cls is None:  # pragma: no cover
        return
    broker.add_middleware(middleware_cls(tracer_provider=None))


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

    original_start = app.start

    async def _start_with_micro_rollback(
        **run_extra_options: Any,  # noqa: ANN401
    ) -> None:
        try:
            await original_start(**run_extra_options)
        except BaseException:
            if micro._exit_stack is not None:  # noqa: SLF001
                await micro.__aexit__(*sys.exc_info())
            raise

    app.start = _start_with_micro_rollback  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    if ambient:
        middleware = type(
            "GrelmicroBrokerMiddleware",
            (_GrelmicroBrokerMiddleware,),
            {"micro": micro},
        )
        app.broker.add_middleware(middleware)  # ty: ignore[unresolved-attribute]
    else:
        micro._on_ambient_disabled()  # noqa: SLF001
    _instrument_broker(app, micro)
