"""Tests for the Grelmicro ASGI context middleware."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.status import HTTP_200_OK

from grelmicro import AmbientBindingError, Grelmicro
from grelmicro.errors import OutOfContextError
from grelmicro.integrations.fastapi import GrelmicroMiddleware
from grelmicro.resilience import RateLimiter, RateLimiterRegistry
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        MutableMapping,
    )
    from typing import Any

pytestmark = [pytest.mark.timeout(5)]


def _stack_names(app: FastAPI) -> list[str]:
    """Return the registered middleware class names, outermost first."""
    return [
        getattr(middleware.cls, "__name__", "unnamed")
        for middleware in app.user_middleware
    ]


def _build_app(*, with_middleware: bool) -> FastAPI:
    """Build a FastAPI app whose handler resolves a RateLimiter ambiently."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
        async with micro:
            yield

    app = FastAPI(lifespan=lifespan)
    if with_middleware:
        app.add_middleware(GrelmicroMiddleware, micro=micro)

    @app.get("/limited")
    async def limited() -> dict[str, bool]:
        limiter = RateLimiter.sliding_window("api", limit=10, window=1.0)
        result = await limiter.acquire(key="client")
        return {"allowed": result.allowed}

    return app


def test_middleware_binds_app_inside_request_handler() -> None:
    """A handler resolves the ambient backend when the middleware is present."""
    app = _build_app(with_middleware=True)
    with TestClient(app) as client:
        response = client.get("/limited")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {"allowed": True}


def test_without_middleware_handler_misses_ambient_backend() -> None:
    """Without the middleware, the handler hits the ambient-miss guard."""
    app = _build_app(with_middleware=False)
    with TestClient(app) as client, pytest.raises(OutOfContextError):
        client.get("/limited")


async def test_middleware_passes_non_request_scopes_through() -> None:
    """The lifespan scope is forwarded untouched."""
    micro = Grelmicro()
    seen: list[str] = []

    async def downstream(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],  # noqa: ARG001
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],  # noqa: ARG001
    ) -> None:
        seen.append(str(scope["type"]))

    middleware = GrelmicroMiddleware(downstream, micro=micro)
    await middleware({"type": "lifespan"}, _noop_receive, _noop_send)
    assert seen == ["lifespan"]


async def test_middleware_resets_binding_after_request() -> None:
    """The current-app binding does not leak past the request scope."""
    micro = Grelmicro()
    from grelmicro._app import _current_micro  # noqa: PLC0415

    async def downstream(
        scope: MutableMapping[str, Any],  # noqa: ARG001
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],  # noqa: ARG001
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],  # noqa: ARG001
    ) -> None:
        assert _current_micro.get() is micro

    middleware = GrelmicroMiddleware(downstream, micro=micro)
    await middleware({"type": "http"}, _noop_receive, _noop_send)
    # Outside the request, the contextvar is restored to unset.
    with pytest.raises(LookupError):
        _current_micro.get()


async def _noop_receive() -> dict[str, object]:
    return {"type": "noop"}


async def _noop_send(message: object) -> None:  # noqa: ARG001
    return None


def _build_installed_app(
    *, ambient: bool = True, custom_lifespan: list[str] | None = None
) -> FastAPI:
    """Build a FastAPI app wired with `micro.install(app)`."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])

    if custom_lifespan is not None:

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
            custom_lifespan.append("enter")
            yield
            custom_lifespan.append("exit")

        app = FastAPI(lifespan=lifespan)
    else:
        app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, bool]:
        limiter = RateLimiter.sliding_window("api", limit=10, window=1.0)
        result = await limiter.acquire(key="client")
        return {"allowed": result.allowed}

    micro.install(app, ambient=ambient)
    return app


def test_install_wires_lifecycle_and_ambient_binding() -> None:
    """`micro.install(app)` opens micro and binds it inside the handler."""
    app = _build_installed_app()
    with TestClient(app) as client:
        response = client.get("/limited")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {"allowed": True}


def test_install_ambient_false_skips_middleware() -> None:
    """`ambient=False` opens micro but does not bind it per request."""
    with pytest.warns(UserWarning, match="ambient=False"):
        app = _build_installed_app(ambient=False)
    with TestClient(app) as client, pytest.raises(OutOfContextError):
        client.get("/limited")


def test_install_chains_existing_lifespan() -> None:
    """A lifespan already passed to FastAPI keeps running around micro."""
    events: list[str] = []
    app = _build_installed_app(custom_lifespan=events)
    with TestClient(app) as client:
        response = client.get("/limited")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {"allowed": True}
    assert events == ["enter", "exit"]


def test_install_keeps_binding_outermost_whatever_the_add_order() -> None:
    """The binding middleware is built outside the ones added around it."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    app = FastAPI()
    app.add_middleware(GZipMiddleware)
    micro.install(app)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

    app.build_middleware_stack()

    assert _stack_names(app) == [
        "GrelmicroMiddleware",
        "TrustedHostMiddleware",
        "GZipMiddleware",
    ]


def test_install_ambient_false_leaves_the_stack_order_alone() -> None:
    """Without the binding middleware there is nothing to keep outermost."""
    micro = Grelmicro()
    app = FastAPI()
    micro.install(app, ambient=False)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

    app.build_middleware_stack()

    assert _stack_names(app) == ["TrustedHostMiddleware"]


def test_install_ambient_false_raises_when_hand_wiring_is_wrapped() -> None:
    """`ambient=False` leaves placement to the reader, and reports it wrong."""
    # A sibling test reimports the integration module, so resolve the class
    # through the live module rather than the one bound at import time.
    binding = importlib.import_module(
        "grelmicro.integrations.fastapi"
    ).GrelmicroMiddleware
    micro = Grelmicro()
    app = FastAPI()
    micro.install(app, ambient=False)
    app.add_middleware(binding, micro=micro)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

    with (
        pytest.raises(AmbientBindingError, match="TrustedHostMiddleware"),
        TestClient(app),
    ):
        pass


def test_install_raises_at_startup_when_the_placement_did_not_hold() -> None:
    """A binding middleware left wrapped fails on startup, not on a request."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    app = FastAPI()
    micro.install(app)
    # Drop the placement hook, as a framework that built its stack another
    # way would, and leave the binding middleware wrapped.
    del app.build_middleware_stack
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

    with (
        pytest.raises(AmbientBindingError, match="TrustedHostMiddleware"),
        TestClient(app),
    ):
        pass


def test_install_rejects_unknown_app() -> None:
    """`micro.install` raises TypeError for an unsupported object."""
    micro = Grelmicro()
    with pytest.raises(TypeError, match="Starlette, FastAPI, and FastStream"):
        micro.install(object())


def test_install_ambient_false_no_warning_without_ambient_components(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """`ambient=False` is silent when no ambient components are registered."""
    micro = Grelmicro()
    app = FastAPI()
    micro.install(app, ambient=False)
    assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


def test_install_ambient_false_strict_raises() -> None:
    """`strict=True` turns the ambient-binding warning into an error."""
    micro = Grelmicro(
        strict=True,
        uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())],
    )
    app = FastAPI()
    with pytest.raises(AmbientBindingError, match="ratelimiter:default"):
        micro.install(app, ambient=False)


def test_check_ambient_binding_true_when_installed() -> None:
    """`check_ambient_binding` is True once the middleware is wired."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    app = FastAPI()
    micro.install(app)
    assert micro.check_ambient_binding(app) is True


def test_check_ambient_binding_false_without_middleware() -> None:
    """`check_ambient_binding` is False when the middleware was never wired."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    app = FastAPI()
    # The footgun: micro opened in a hand-written lifespan, install never
    # called, so no GrelmicroMiddleware was added.
    assert micro.check_ambient_binding(app) is False


def test_check_ambient_binding_catches_an_uninstalled_mounted_app() -> None:
    """A mounted sub-app that forgot `install` is caught by the check.

    The mount is the one place a forgotten `install` does not raise on the
    first request. The host's request scope is still bound inside the
    sub-application, so it resolves against the host's components instead of
    its own, silently. The check is what turns that back into a signal.
    """
    host_micro = Grelmicro(
        uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())]
    )
    sub_micro = Grelmicro(
        uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())]
    )
    host = FastAPI()
    host_micro.install(host)
    sub = FastAPI()  # install(sub) deliberately forgotten
    host.mount("/sub", sub)

    assert host_micro.check_ambient_binding(host) is True
    assert sub_micro.check_ambient_binding(sub) is False


def test_check_ambient_binding_true_without_ambient_components() -> None:
    """Nothing needs binding, so the check passes regardless of middleware."""
    micro = Grelmicro()
    app = FastAPI()
    assert micro.check_ambient_binding(app) is True


def test_check_ambient_binding_rejects_unknown_app() -> None:
    """`check_ambient_binding` raises for an unsupported app with ambient components."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    with pytest.raises(TypeError, match="Starlette, FastAPI, and FastStream"):
        micro.check_ambient_binding(object())
