"""Tests for registering the idempotency middleware through `uses=[...]`."""

from __future__ import annotations

import warnings
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

import pytest
from fastapi import APIRouter, FastAPI, Response
from fastapi import Request as FastAPIRequest
from fastapi.testclient import TestClient
from litestar import Litestar, asgi, post
from litestar import Request as LitestarRequest
from litestar import Response as LitestarResponse
from litestar import Router as LitestarRouter
from litestar.connection import ASGIConnection
from litestar.di import NamedDependency, Provide
from litestar.exceptions import (
    HTTPException,
    NotAuthorizedException,
    ValidationException,
)
from litestar.handlers import BaseRouteHandler
from litestar.middleware import DefineMiddleware
from litestar.params import FromPath
from litestar.response.base import ASGIResponse
from litestar.testing import TestClient as LitestarTestClient
from litestar.types import Receive as LitestarReceive
from litestar.types import Scope as LitestarScope
from litestar.types import Send as LitestarSend
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route, Router

from grelmicro import (
    Grelmicro,
    GrelmicroMiddleware,
    MiddlewarePlacementWarning,
    Usable,
)
from grelmicro._paths import _routing_app
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.errors import SettingsValidationError
from grelmicro.http import (
    CachedResponses,
    IdempotencyMiddleware,
    IdempotentRequests,
    IdempotentRequestsConfig,
)
from grelmicro.http._idempotency import (
    _checked_key,
    _GatedRoutes,
)
from grelmicro.idempotency import Idempotency
from grelmicro.idempotency.errors import IdempotencyKeyFunctionError
from grelmicro.integrations import litestar as litestar_integration
from grelmicro.integrations.fastapi import CachedResponse
from grelmicro.integrations.litestar import Anonymous
from grelmicro.integrations.starlette import install_middleware
from grelmicro.providers.memory import MemoryProvider
from tests.test_route_gate_litestar import Passing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from starlette.requests import Request
    from starlette.types import Scope

pytestmark = [pytest.mark.timeout(5)]

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
_NOT_A_STRING: Any = b"Idempotency-Key"
MAX_BODY_SIZE = 2048
MAX_CHAIN = 8
HTTP_400_BAD_REQUEST = 400
HTTP_422_UNPROCESSABLE_CONTENT = 422
MAX_CHAIN = 8
HTTP_400_BAD_REQUEST = 400
HTTP_401_UNAUTHORIZED = 401
HTTP_500_INTERNAL_SERVER_ERROR = 500
HTTP_200_OK = 200
HTTP_402_PAYMENT_REQUIRED = 402


def _charge_app(*components: Usable) -> tuple[FastAPI, Grelmicro]:
    """Build a FastAPI app with one charge route and the given components."""
    micro = Grelmicro(uses=[MemoryProvider(), *components])
    app = FastAPI()

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)
    return app, micro


def test_a_registered_component_replays_a_repeated_key() -> None:
    """`uses=[IdempotentRequests()]` is the whole wiring."""
    # Arrange
    app, _micro = _charge_app(IdempotentRequests())

    # Act
    with TestClient(app) as client:
        first = client.post("/charge", headers={HEADER: "abc"})
        second = client.post("/charge", headers={HEADER: "abc"})

    # Assert
    assert first.json() == second.json()
    assert HEADER not in first.headers
    assert second.headers["idempotent-replayed"] == "true"


def test_the_bare_form_stores_under_the_http_namespace() -> None:
    """`IdempotentRequests()` carries its own settings, and needs none."""
    # Act
    _middleware, options = IdempotentRequests().asgi_middleware()

    # Assert
    assert options["idempotency"].name == "http"
    config = options["live"].state.config
    assert config.key_header == HEADER
    assert config.replay_header == REPLAY_HEADER
    assert config.methods == ("POST",)


def _header_of(middleware: Any) -> str:  # noqa: ANN401
    """Return the key header one installed middleware answers for.

    A registered component hands its middleware the snapshot cell rather
    than the values, so what it reads is in there. The binding
    middleware carries neither and is named as itself.
    """
    live = middleware.kwargs.get("live")
    return "binding" if live is None else live.state.config.key_header


def test_the_component_forwards_every_middleware_option() -> None:
    """What the component takes is what the middleware is built with."""

    # Arrange
    def skip(response: Any) -> bool:  # noqa: ANN401, ARG001
        return False

    component = IdempotentRequests(
        ttl=30,
        namespace="payments",
        key_header="X-Idempotency-Key",
        replay_header="X-Idempotent-Replayed",
        methods=("POST", "PATCH"),
        skip=skip,
        require_key=True,
        fingerprint_body=True,
        max_body_size=MAX_BODY_SIZE,
        max_wait=1.0,
    )

    # Act
    middleware, options = component.asgi_middleware()

    # Assert
    assert middleware is IdempotencyMiddleware
    assert options["idempotency"].name == "payments"
    assert options["skip"] is skip
    # The values reach the middleware through the snapshot cell, so a
    # live reconfigure changes what it answers with without the
    # middleware stack being rebuilt.
    config = options["live"].state.config
    assert config.key_header == "X-Idempotency-Key"
    assert config.replay_header == "X-Idempotent-Replayed"
    assert config.methods == ("POST", "PATCH")
    assert config.require_key is True
    assert config.fingerprint_body is True
    assert config.max_body_size == MAX_BODY_SIZE
    assert config.max_wait == 1.0


def test_a_custom_replay_header_marks_the_replay() -> None:
    """No standard names the header, so a service picks what its clients read."""
    # Arrange
    app, _micro = _charge_app(
        IdempotentRequests(replay_header="X-Idempotent-Replayed")
    )

    # Act
    with TestClient(app) as client:
        client.post("/charge", headers={HEADER: "abc"})
        second = client.post("/charge", headers={HEADER: "abc"})

    # Assert
    assert second.headers["x-idempotent-replayed"] == "true"
    assert REPLAY_HEADER.lower() not in second.headers


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: IdempotentRequests(key_header="Idempotency Key"),
            id="key-header-space",
        ),
        pytest.param(
            lambda: IdempotentRequests(replay_header=""),
            id="replay-header-empty",
        ),
        pytest.param(
            lambda: IdempotentRequests(replay_header="Rejeu-Idempotent\xe9"),
            id="non-ascii",
        ),
        pytest.param(
            lambda: IdempotentRequests(key_header="Idempotency-Key\n"),
            id="trailing-newline",
        ),
        pytest.param(
            lambda: IdempotentRequests(replay_header="Content-Length"),
            id="frames-the-response",
        ),
        pytest.param(
            lambda: IdempotentRequests(replay_header="Content-Type"),
            id="labels-the-response",
        ),
        pytest.param(
            lambda: IdempotentRequests(replay_header="ETag"),
            id="caches-the-response",
        ),
        pytest.param(
            lambda: IdempotentRequests(replay_header="Retry-After"),
            id="paces-the-client",
        ),
        pytest.param(
            lambda: IdempotentRequests(replay_header="Content-Disposition"),
            id="names-the-download",
        ),
        pytest.param(
            lambda: IdempotentRequests(key_header=_NOT_A_STRING),
            id="not-a-string",
        ),
        pytest.param(
            lambda: IdempotentRequests(key_header="Content-Type"),
            id="every-request-carries-it",
        ),
    ],
)
def test_a_header_name_that_cannot_reach_the_wire_is_refused(
    build: Callable[[], IdempotentRequests],
) -> None:
    """A broken name is an argument error, not a broken response."""
    # Act / Assert
    with pytest.raises(SettingsValidationError):
        build()


def test_the_middleware_refuses_a_broken_header_name_of_its_own() -> None:
    """Added by hand, it checks the names the component would have checked."""
    # Arrange
    app = FastAPI()

    # Act / Assert
    with pytest.raises(SettingsValidationError):
        IdempotencyMiddleware(
            app,
            idempotency=Idempotency("http"),
            replay_header="Idempotent Replayed",
        )


def test_the_replay_marker_is_the_only_value_under_its_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The marker means a replay, so a fresh response never carries it."""
    # Arrange
    micro = Grelmicro(
        uses=[MemoryProvider(), IdempotentRequests(replay_header="X-Cache")]
    )
    app = FastAPI()

    @app.post("/charge")
    async def charge(response: Response) -> dict[str, int]:
        # The marker's own value, which is what a handler cannot claim.
        response.headers["X-Cache"] = "true"
        return {"amount": 100}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        first = client.post("/charge", headers={HEADER: "abc"})
        second = client.post("/charge", headers={HEADER: "abc"})
        third = client.post("/charge", headers={HEADER: "abc"})

    # Assert
    assert "x-cache" not in first.headers
    assert second.headers["x-cache"] == "true"
    assert third.headers["x-cache"] == "true"
    # Static configuration, so it is said once and not once a request.
    assert caplog.text.count("The X-Cache header a response carried") == 1


def test_install_documents_the_middleware_in_the_schema() -> None:
    """The header a client has to send reaches the generated schema."""
    # Arrange
    app, _micro = _charge_app(IdempotentRequests())

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert [p["name"] for p in operation["parameters"]] == [HEADER]
    assert "409" in operation["responses"]


def test_openapi_false_keeps_one_set_of_rules_out_of_the_schema() -> None:
    """A quiet component stays unpublished while a loud one is described."""
    # Arrange
    app, _micro = _charge_app(
        IdempotentRequests(
            name="quiet",
            key_header="X-Quiet-Key",
            replay_header="X-Quiet-Replayed",
            openapi=False,
        ),
        IdempotentRequests(
            name="loud",
            key_header="X-Loud-Key",
            replay_header="X-Loud-Replayed",
        ),
    )

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert [p["name"] for p in operation["parameters"]] == ["X-Loud-Key"]
    assert list(operation["responses"]["200"]["headers"]) == ["X-Loud-Replayed"]


def test_openapi_false_leaves_the_schema_alone() -> None:
    """A service that publishes its own schema keeps it untouched."""
    # Arrange
    app, _micro = _charge_app(IdempotentRequests(openapi=False))

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert "parameters" not in operation
    assert "409" not in operation["responses"]


def test_registration_order_is_wrapping_order() -> None:
    """The first one registered is the outermost, and the binding is above both."""
    # Arrange
    app, _micro = _charge_app(
        IdempotentRequests(namespace="outer", key_header="X-Outer"),
        IdempotentRequests(
            namespace="inner", key_header="X-Inner", name="second"
        ),
    )

    # Act
    app.build_middleware_stack()

    # Assert
    added = [_header_of(middleware) for middleware in app.user_middleware]
    assert added == ["binding", "X-Outer", "X-Inner"]


def _layers(handler: Any, app: Litestar) -> list[Any]:  # noqa: ANN401
    """Return the layers of a Litestar handler chain, down to the router."""
    layers = []
    while handler is not None and handler is not app and callable(handler):
        layers.append(handler)
        handler = getattr(handler, "app", None)
    return layers


def test_litestar_wraps_the_middleware_inside_what_renders_errors() -> None:
    """It runs in the request scope, and under Litestar's error handling.

    Wrapping around the error handling would hand it the framework's `500`
    as though the app had produced it, and the replay would serve that
    `500` for the whole window.
    """

    # Arrange
    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = Litestar(route_handlers=[charge])

    # Act
    micro.install(app)

    # Assert
    binding = app.asgi_handler
    assert isinstance(binding, GrelmicroMiddleware)
    # Under the layer that turns an exception into a response, not around it.
    layers = [type(layer).__name__ for layer in _layers(binding, app)]
    assert layers.index("ExceptionHandlerMiddleware") < layers.index(
        "IdempotencyMiddleware"
    )


class _Copying(Passing):
    """An app middleware that hands the request on in a copy of its scope."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        await self.app({**scope}, receive, send)


class _DeclinedError(Exception):
    """An error the app answers with a handler of its own."""


def _declined(request: Any, exc: Exception) -> LitestarResponse[str]:  # noqa: ANN401, ARG001
    return LitestarResponse("declined", status_code=HTTP_402_PAYMENT_REQUIRED)


def _crashed(request: Any, exc: Exception) -> LitestarResponse[str]:  # noqa: ANN401, ARG001
    return LitestarResponse(
        "crashed", status_code=HTTP_500_INTERNAL_SERVER_ERROR
    )


_DECLARED_IDEMPOTENCY = DefineMiddleware(
    IdempotencyMiddleware,  # ty: ignore[invalid-argument-type]
    idempotency=Idempotency("http"),
)
"""The idempotency middleware as an app declares it in its own stack."""

_WITH_APP_MIDDLEWARE = [
    pytest.param([Passing], id="app middleware"),
    pytest.param([_Copying], id="copying app middleware"),
    pytest.param([_DECLARED_IDEMPOTENCY], id="declared by the app"),
]
"""Middleware lists of a Litestar app that declares middleware of its own."""

_ANY_MIDDLEWARE = [
    pytest.param([], id="no app middleware"),
    *_WITH_APP_MIDDLEWARE,
]
"""Middleware lists of a Litestar app, none included."""


def _litestar_app(
    handler: Any,  # noqa: ANN401
    middleware: list[Any],
    catch_all: dict[Any, Any] | None = None,
) -> Litestar:
    """Return an installed Litestar app serving `handler` behind `middleware`.

    `catch_all` adds the exception handlers the app answers a crash with.
    """
    app = Litestar(
        route_handlers=[handler],
        middleware=middleware,
        exception_handlers={_DeclinedError: _declined, **(catch_all or {})},
    )
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MiddlewarePlacementWarning)
        micro.install(app)
    return app


@pytest.mark.parametrize("middleware", _ANY_MIDDLEWARE)
def test_litestar_never_replays_an_unhandled_exception(
    middleware: list[Any],
) -> None:
    """The framework's `500` is not a response the app chose to store.

    With app middleware, Litestar renders the crash inside the route,
    before the middleware sees the exception.
    """
    # Arrange
    calls: list[int] = []

    @post("/boom", status_code=200)
    async def boom() -> dict[str, int]:
        calls.append(1)
        msg = "kaboom"
        raise RuntimeError(msg)

    app = _litestar_app(boom, middleware)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        first = client.post("/boom", headers={HEADER: "abc"})
        second = client.post("/boom", headers={HEADER: "abc"})

    # Assert
    assert first.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert second.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert "idempotent-replayed" not in second.headers
    # Ran again, exactly as it does on Starlette and FastAPI.
    assert calls == [1, 1]


@pytest.mark.parametrize("middleware", _ANY_MIDDLEWARE)
@pytest.mark.parametrize(
    "catch_all",
    [
        {Exception: _crashed},
        {BaseException: _crashed},
        {HTTP_500_INTERNAL_SERVER_ERROR: _crashed},
    ],
    ids=["exception handler", "base exception handler", "500 handler"],
)
def test_litestar_never_replays_a_crash_its_catch_all_renders(
    middleware: list[Any], catch_all: dict[Any, Any]
) -> None:
    """A catch-all handler renders a crash, which stays unhandled."""
    # Arrange
    calls: list[int] = []

    @post("/boom", status_code=200)
    async def boom() -> dict[str, int]:
        calls.append(1)
        msg = "kaboom"
        raise RuntimeError(msg)

    app = _litestar_app(boom, middleware, catch_all)

    # Act
    with LitestarTestClient(app=app) as client:
        client.post("/boom", headers={HEADER: "abc"})
        second = client.post("/boom", headers={HEADER: "abc"})

    # Assert
    assert second.text == "crashed"
    assert "idempotent-replayed" not in second.headers
    assert calls == [1, 1]


@pytest.mark.parametrize("middleware", _ANY_MIDDLEWARE)
def test_litestar_replays_a_500_the_handler_returns(
    middleware: list[Any],
) -> None:
    """A `500` the handler returned is its answer for the key."""
    # Arrange
    calls: list[int] = []

    @post("/fail")
    async def fail() -> LitestarResponse[str]:
        calls.append(1)
        return LitestarResponse(
            "failed", status_code=HTTP_500_INTERNAL_SERVER_ERROR
        )

    app = _litestar_app(fail, middleware)

    # Act
    with LitestarTestClient(app=app) as client:
        client.post("/fail", headers={HEADER: "abc"})
        second = client.post("/fail", headers={HEADER: "abc"})

    # Assert
    assert second.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert second.text == "failed"
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == [1]


@pytest.mark.parametrize("middleware", _ANY_MIDDLEWARE)
@pytest.mark.parametrize(
    ("raised", "status"),
    [
        (HTTPException(status_code=409, detail="busy"), 409),
        (ValidationException(detail="bad"), 400),
        (_DeclinedError(), HTTP_402_PAYMENT_REQUIRED),
    ],
    ids=["http exception", "validation exception", "handled exception"],
)
def test_litestar_replays_a_handled_exception(
    middleware: list[Any], raised: Exception, status: int
) -> None:
    """An exception the app answers on purpose is the handler's answer.

    An `HTTPException`, its subclasses, and an exception the app registered
    a handler for are stored and replayed like a returned response.
    """
    # Arrange
    calls: list[int] = []

    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        calls.append(1)
        raise raised

    app = _litestar_app(charge, middleware)

    # Act
    with LitestarTestClient(app=app) as client:
        client.post("/charge", headers={HEADER: "abc"})
        second = client.post("/charge", headers={HEADER: "abc"})

    # Assert
    assert second.status_code == status
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == [1]


def _starlette_app(framework: str) -> Starlette:
    """Return an installed Starlette or FastAPI app that crashes or fails."""

    async def boom(request: Request) -> JSONResponse:  # noqa: ARG001
        calls.append("boom")
        msg = "kaboom"
        raise RuntimeError(msg)

    async def fail(request: Request) -> JSONResponse:  # noqa: ARG001
        calls.append("fail")
        return JSONResponse(
            {"failed": True}, status_code=HTTP_500_INTERNAL_SERVER_ERROR
        )

    calls: list[str] = []
    routes: list[BaseRoute] = [
        Route("/boom", boom, methods=["POST"]),
        Route("/fail", fail, methods=["POST"]),
    ]
    app = (
        FastAPI(routes=routes)
        if framework == "fastapi"
        else Starlette(routes=routes)
    )
    app.state.calls = calls
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)
    return app


@pytest.mark.parametrize("framework", ["starlette", "fastapi"])
def test_an_unhandled_exception_stores_nothing(framework: str) -> None:
    """A crash runs the handler again on the retry."""
    # Arrange
    app = _starlette_app(framework)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/boom", headers={HEADER: "abc"})
        second = client.post("/boom", headers={HEADER: "abc"})

    # Assert
    assert second.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert "idempotent-replayed" not in second.headers
    assert app.state.calls == ["boom", "boom"]


@pytest.mark.parametrize("framework", ["starlette", "fastapi"])
def test_a_500_the_handler_returns_is_replayed(framework: str) -> None:
    """A returned `500` is the handler's answer for the key."""
    # Arrange
    app = _starlette_app(framework)

    # Act
    with TestClient(app) as client:
        client.post("/fail", headers={HEADER: "abc"})
        second = client.post("/fail", headers={HEADER: "abc"})

    # Assert
    assert second.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert second.headers["idempotent-replayed"] == "true"
    assert app.state.calls == ["fail"]


def test_litestar_replays_a_repeated_key() -> None:
    """The wrapped middleware resolves its cache and replays."""

    # Arrange
    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = Litestar(route_handlers=[charge])
    micro.install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        first = client.post("/charge", headers={HEADER: "abc"})
        second = client.post("/charge", headers={HEADER: "abc"})

    # Assert
    assert first.json() == second.json()
    assert second.headers["idempotent-replayed"] == "true"


def test_install_middleware_wires_an_app_that_never_went_through_install() -> (
    None
):
    """The integration hook is callable on its own, like the others."""
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider()])
    component = IdempotentRequests()

    async def charge(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"amount": 100})

    app = Starlette(routes=[Route("/charge", charge, methods=["POST"])])
    app.add_middleware(GrelmicroMiddleware, micro=micro)

    # Act
    install_middleware(app, [component])

    # Assert
    assert [middleware.cls for middleware in app.user_middleware] == [
        GrelmicroMiddleware,
        IdempotencyMiddleware,
    ]


def test_litestar_install_middleware_alone_never_replays_a_crash() -> None:
    """Called without `install`, it still stores nothing for an unhandled exception."""
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider()])
    component = IdempotentRequests()
    calls: list[int] = []

    @post("/boom", status_code=200)
    async def boom() -> dict[str, int]:
        calls.append(1)
        msg = "kaboom"
        raise RuntimeError(msg)

    @asynccontextmanager
    async def opened(app: Litestar) -> AsyncIterator[None]:  # noqa: ARG001
        async with micro:
            yield

    app = Litestar(route_handlers=[boom], lifespan=[opened])
    app.asgi_handler = cast(
        "Any", GrelmicroMiddleware(cast("Any", app.asgi_handler), micro=micro)
    )
    litestar_integration.install_middleware(app, [component])

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        client.post("/boom", headers={HEADER: "abc"})
        second = client.post("/boom", headers={HEADER: "abc"})

    # Assert
    assert second.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert "idempotent-replayed" not in second.headers
    assert calls == [1, 1]


def test_a_component_without_middleware_is_left_alone() -> None:
    """Only a component that asks for one gets one."""
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider()])
    app = FastAPI()

    # Act
    micro.install(app)

    # Assert
    assert [middleware.cls for middleware in app.user_middleware] == [
        GrelmicroMiddleware
    ]


def test_the_reuse_status_follows_the_draft_by_default() -> None:
    """`422` is what the Idempotency-Key header draft asks for."""
    # Arrange
    app, _micro = _charge_app(IdempotentRequests(fingerprint_body=True))

    # Act
    with TestClient(app) as client:
        client.post("/charge", headers={HEADER: "abc"}, json={"amount": 100})
        reused = client.post(
            "/charge", headers={HEADER: "abc"}, json={"amount": 999}
        )

    # Assert
    assert reused.status_code == HTTP_422_UNPROCESSABLE_CONTENT
    assert reused.json()["type"].endswith("#idempotency-key-reused")


def test_the_reuse_status_is_configurable() -> None:
    """A service whose clients were built against Stripe answers `400`."""
    # Arrange
    app, _micro = _charge_app(
        IdempotentRequests(fingerprint_body=True, reused_status=400)
    )

    # Act
    with TestClient(app) as client:
        client.post("/charge", headers={HEADER: "abc"}, json={"amount": 100})
        reused = client.post(
            "/charge", headers={HEADER: "abc"}, json={"amount": 999}
        )

    # Assert
    assert reused.status_code == HTTP_400_BAD_REQUEST
    # The identifier is what a client branches on, and it does not move.
    assert reused.json()["type"].endswith("#idempotency-key-reused")


def test_the_schema_publishes_the_configured_reuse_status() -> None:
    """What the schema promises is what the wire returns."""
    # Arrange
    app, _micro = _charge_app(
        IdempotentRequests(fingerprint_body=True, reused_status=400)
    )

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert "422" not in operation["responses"]
    assert (
        "different request payload"
        in operation["responses"]["400"]["description"]
    )


class _Marker:
    """A pure-ASGI middleware that marks the response it passed through."""

    def __init__(self, app: Any, *, value: str) -> None:  # noqa: ANN401
        self.app = app
        self.value = value

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        await self.app(scope, receive, send)


class _Marked:
    """A component asking for a middleware and nothing else.

    No `_document_openapi`, which is the case of a middleware that has
    nothing to say about an OpenAPI schema, and of every component a third
    party ships against a released `Integration` protocol.
    """

    kind = "marked"

    def __init__(self, *, value: str = "marked") -> None:
        self._value = value

    @property
    def name(self) -> str:
        return "default"

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        return _Marker, {"value": self._value}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_a_component_that_documents_nothing_is_still_wired() -> None:
    """`_document_openapi` is optional, like every feature-detected hook."""
    # Arrange
    app, _micro = _charge_app(_Marked())

    # Act
    app.build_middleware_stack()

    # Assert
    assert [middleware.cls for middleware in app.user_middleware] == [
        GrelmicroMiddleware,
        _Marker,
    ]


def test_litestar_wraps_the_handler_when_there_is_no_binding() -> None:
    """`ambient=False` leaves no binding, and the middleware still lands."""

    # Arrange
    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    # No provider, so nothing resolves ambiently and `ambient=False` is a
    # placement the app can make without a warning.
    micro = Grelmicro(uses=[_Marked()])
    app = Litestar(route_handlers=[charge])

    # Act
    micro.install(app, ambient=False)

    # Assert
    assert any(
        isinstance(layer, _Marker) for layer in _layers(app.asgi_handler, app)
    )


def test_the_ttl_needs_no_pattern_object() -> None:
    """The common case sets a lifetime, not an `Idempotency`."""
    # Act
    _middleware, options = IdempotentRequests(ttl=3600).asgi_middleware()

    # Assert
    assert options["idempotency"].name == "http"
    assert options["idempotency"].config.ttl == timedelta(hours=1)


def test_the_store_is_reachable_for_the_code_that_needs_it() -> None:
    """The component owns the `Idempotency`, and hands it over on request."""
    # Arrange
    component = IdempotentRequests(ttl=30, namespace="payments")

    # Act / Assert
    assert component.idempotency.name == "payments"


def test_an_excluded_path_is_never_replayed() -> None:
    """`exclude` is the same word, and the same matching, as everywhere."""
    # Arrange
    app, _micro = _charge_app(IdempotentRequests(exclude=("/charge",)))

    # Act
    with TestClient(app) as client:
        first = client.post("/charge", headers={HEADER: "abc"})
        second = client.post("/charge", headers={HEADER: "abc"})

    # Assert
    assert "idempotent-replayed" not in first.headers
    assert "idempotent-replayed" not in second.headers


def test_include_selects_a_router_by_its_prefix() -> None:
    """Grouping endpoints is what a router is for, so selection follows it."""
    # Arrange
    micro = Grelmicro(
        uses=[MemoryProvider(), IdempotentRequests(include=("/payments/*",))]
    )
    app = FastAPI()
    payments = APIRouter(prefix="/payments")

    @payments.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    @app.post("/other")
    async def other() -> dict[str, int]:
        return {"amount": 100}

    app.include_router(payments)
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.post("/payments/charge", headers={HEADER: "abc"})
        inside = client.post("/payments/charge", headers={HEADER: "abc"})
        client.post("/other", headers={HEADER: "abc"})
        outside = client.post("/other", headers={HEADER: "abc"})

    # Assert
    assert inside.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in outside.headers


def test_exclude_carves_a_route_out_of_an_included_router() -> None:
    """The two rules do not fight: `exclude` wins."""
    # Arrange
    micro = Grelmicro(
        uses=[
            MemoryProvider(),
            IdempotentRequests(
                include=("/payments/*",), exclude=("/payments/webhook",)
            ),
        ]
    )
    app = FastAPI()

    @app.post("/payments/webhook")
    async def webhook() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.post("/payments/webhook", headers={HEADER: "abc"})
        second = client.post("/payments/webhook", headers={HEADER: "abc"})

    # Assert
    assert "idempotent-replayed" not in second.headers


class _RequireToken:
    """Refuse every request that carries no token, as an app's auth would."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self.app = app
        self.seen = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] != "http":  # pragma: no cover
            await self.app(scope, receive, send)
            return
        self.seen += 1
        headers = dict(scope["headers"])
        if headers.get(b"authorization") != b"token":
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


def test_a_replay_never_skips_the_app_authentication() -> None:
    """A private request runs through authentication and the app every time.

    A middleware of ours that answers without calling the app must never be
    the reason a request skipped authentication. Registering the component
    after the app added its own middleware is the natural order, and it is
    the order that would break this if the placement were wrong.
    """
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = FastAPI()
    calls = 0

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"call": calls}

    app.add_middleware(_RequireToken)
    micro.install(app)

    # Act
    with TestClient(app) as client:
        authorized = client.post(
            "/charge",
            headers={HEADER: "abc", "Authorization": "token"},
        )
        stolen = client.post("/charge", headers={HEADER: "abc"})
        replayed = client.post(
            "/charge",
            headers={HEADER: "abc", "Authorization": "token"},
        )

    # Assert
    assert authorized.status_code == HTTP_200_OK
    # The key alone buys nothing: the caller is turned away as it would be
    # on a first request, and never sees the stored body.
    assert stolen.status_code == HTTP_401_UNAUTHORIZED
    assert stolen.content == b""
    assert replayed.json() == {"call": 2}
    assert "idempotent-replayed" not in replayed.headers


def test_an_identity_aware_key_makes_private_requests_idempotent() -> None:
    """A custom key explicitly opts authenticated requests into replay."""
    # Arrange
    micro = Grelmicro(
        uses=[
            MemoryProvider(),
            IdempotentRequests(
                key=lambda scope, key: (
                    f"verified-user\x1f{scope['path']}\x1f{key}"
                )
            ),
        ]
    )
    app = FastAPI()
    app.add_middleware(_RequireToken)
    calls = 0

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"call": calls}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        first = client.post(
            "/charge",
            headers={HEADER: "abc", "Authorization": "token"},
        )
        replayed = client.post(
            "/charge",
            headers={HEADER: "abc", "Authorization": "token"},
        )

    # Assert
    assert first.json() == {"call": 1}
    assert replayed.json() == {"call": 1}
    assert replayed.headers["idempotent-replayed"] == "true"


def test_grelmicro_middleware_stays_outside_and_the_rest_inside() -> None:
    """The binding wraps everything, and ours sit closest to the handler."""
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = FastAPI()
    app.add_middleware(_RequireToken)

    # Act
    micro.install(app)
    app.build_middleware_stack()

    # Assert
    assert [middleware.cls for middleware in app.user_middleware] == [
        GrelmicroMiddleware,
        _RequireToken,
        IdempotencyMiddleware,
    ]


def test_litestar_warns_when_the_wrap_sits_outside_app_middleware() -> None:
    """Litestar builds its stack at construction, so `install` can only wrap.

    A middleware of ours answering a request would then answer before the
    app's own middleware, authentication included. That is worth saying out
    loud, with the wiring that fixes it.
    """
    # Arrange
    from litestar.middleware import ASGIMiddleware  # noqa: PLC0415

    class Auth(ASGIMiddleware):
        async def handle(
            self,
            scope: Any,  # noqa: ANN401
            receive: Any,  # noqa: ANN401
            send: Any,  # noqa: ANN401
            next_app: Any,  # noqa: ANN401
        ) -> None:
            await next_app(scope, receive, send)  # pragma: no cover

    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        return {"amount": 100}  # pragma: no cover

    app = Litestar(route_handlers=[charge], middleware=[Auth()])
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])

    # Act / Assert
    with pytest.warns(MiddlewarePlacementWarning, match="Litestar"):
        micro.install(app)


def test_litestar_security_probe_needs_no_starlette(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Litestar app's checks are matched without Starlette's route compiler."""
    # Arrange
    import sys  # noqa: PLC0415

    @post("/items/{item_id:int}", guards=[_caller_guard])
    async def item(item_id: FromPath[int]) -> int:
        return item_id

    @post("/files/{name:path}", guards=[_caller_guard])
    async def file(name: FromPath[str]) -> str:
        return name

    app = Litestar(route_handlers=[item, file])
    monkeypatch.setitem(sys.modules, "starlette.routing", None)
    gated = _GatedRoutes()

    # Act
    gated.read(app)

    # Assert
    assert gated.matches_route("POST", "/items/7")
    assert gated.matches_route("POST", "/files/a/b.txt")
    assert not gated.matches_route("POST", "/items/7/more")
    assert not gated.matches_route("GET", "/items/7")


def test_security_probe_handles_repeated_mounted_application() -> None:
    """The same mounted app is read once and cannot form a loop."""
    # Arrange
    child = Starlette()
    app = Starlette()
    app.mount("/one", child)
    app.mount("/two", child)
    gated = _GatedRoutes()

    # Act
    gated.read(app)

    # Assert
    assert not gated.matches_route("POST", "/one")


def test_security_probe_handles_an_asgi_middleware_loop() -> None:
    """A malformed middleware cycle terminates without inventing routes."""

    # Arrange
    class Loop:
        app: Any

    loop = Loop()
    loop.app = loop

    # Act / Assert
    gated = _GatedRoutes()
    gated.read(loop)
    assert _routing_app(loop) is loop
    assert not gated.matches_route("POST", "/")


def test_litestar_leaves_a_middleware_the_app_already_wired() -> None:
    """Wired at construction is the better place, and one is enough."""

    # Arrange
    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    app = Litestar(
        route_handlers=[charge],
        middleware=[
            DefineMiddleware(
                IdempotencyMiddleware,  # ty: ignore[invalid-argument-type]
                idempotency=Idempotency("http"),
            )
        ],
    )
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])

    # Act
    micro.install(app)

    # Assert
    binding = app.asgi_handler
    assert isinstance(binding, GrelmicroMiddleware)
    # Not wrapped a second time: the one the app wired is the one that runs.
    assert not isinstance(binding.app, IdempotencyMiddleware)
    with LitestarTestClient(app=app) as client:
        client.post("/charge", headers={HEADER: "abc"})
        replay = client.post("/charge", headers={HEADER: "abc"})
    assert replay.headers["idempotent-replayed"] == "true"


def test_idempotency_middleware_hostile_key_is_named_not_printed() -> None:
    """What a key function returns is caller data, and reading it runs code."""

    # Arrange
    class Unbound:
        @property
        def __class__(self) -> type:
            msg = "unbound proxy"
            raise RuntimeError(msg)

        def __repr__(self) -> str:
            msg = "no repr for you"
            raise RuntimeError(msg)

    # Act / Assert
    with pytest.raises(IdempotencyKeyFunctionError, match="expected a"):
        _checked_key(Unbound(), "abc")


def test_a_hand_added_middleware_is_not_added_twice() -> None:
    """An app that wired it itself placed it where it wanted."""
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = FastAPI()

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))

    # Act
    micro.install(app)

    # Assert
    assert [middleware.cls for middleware in app.user_middleware] == [
        GrelmicroMiddleware,
        IdempotencyMiddleware,
    ]


async def test_a_key_holding_bytes_a_header_cannot_carry_is_refused() -> None:
    """The schema publishes printable ASCII, so the wire enforces it.

    Driven as raw ASGI: an HTTP client refuses to send these, and a proxy
    or a hand-rolled client does not.
    """
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    reached: list[int] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        reached.append(1)  # pragma: no cover
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    middleware, options = IdempotentRequests().asgi_middleware()
    wrapped = middleware(app, **options)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    # Act
    async with micro:
        for raw in (b"abc\x01def", b"abc\x1fdef", b"key-\xff"):
            sent.clear()
            await wrapped(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/charge",
                    "headers": [(b"idempotency-key", raw)],
                    "query_string": b"",
                },
                receive,
                send,
            )
            # Assert
            assert sent[0]["status"] == HTTP_400_BAD_REQUEST, raw
    assert reached == []


def test_installing_after_the_app_started_says_so() -> None:
    """The list this edits stops being the one that serves requests.

    `micro.install(app)` fails on the binding first, and a direct call to
    `install_middleware` has no such guard in front of it. Silence there
    would look installed and answer nothing.
    """
    # Arrange
    app = FastAPI()

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}  # pragma: no cover

    # What serving the first request does.
    app.middleware_stack = app.build_middleware_stack()

    # Act / Assert
    with pytest.raises(RuntimeError, match="after an application has started"):
        install_middleware(app, [IdempotentRequests()])
    with pytest.raises(RuntimeError, match="after an application has started"):
        Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)


def test_installing_twice_wires_one_of_each() -> None:
    """A second `install` is a wiring mistake, not a second middleware.

    Two bindings set the same context variable twice per request, and two
    idempotency layers store, capture and answer twice.
    """
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = FastAPI()

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}  # pragma: no cover

    # Act
    micro.install(app)
    micro.install(app)

    # Assert
    assert [middleware.cls for middleware in app.user_middleware] == [
        GrelmicroMiddleware,
        IdempotencyMiddleware,
    ]


def test_installing_twice_on_litestar_wires_one_of_each() -> None:
    """The same, where the wiring is a chain rather than a list."""

    # Arrange
    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        return {"amount": 100}  # pragma: no cover

    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = Litestar(route_handlers=[charge])

    # Act
    micro.install(app)
    micro.install(app)

    # Assert
    layers: list[str] = []
    handler: object = app.asgi_handler
    while handler is not None and len(layers) < MAX_CHAIN:
        layers.append(type(handler).__name__)
        handler = getattr(handler, "app", None)
    assert layers.count("IdempotencyMiddleware") == 1
    assert layers.count("GrelmicroMiddleware") == 1


def test_litestar_stays_under_the_error_layer_with_cors_configured() -> None:
    """One hop is not enough when the app configured its own outer layers.

    With CORS in front, taking a single hop lands above the layer that
    renders exceptions, and the framework's `500` becomes a stored
    response that replays for the whole window.
    """
    # Arrange
    from litestar.config.cors import CORSConfig  # noqa: PLC0415

    calls: list[int] = []

    @post("/boom", status_code=200)
    async def boom() -> dict[str, int]:
        calls.append(1)
        msg = "kaboom"
        raise RuntimeError(msg)

    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = Litestar(
        route_handlers=[boom],
        cors_config=CORSConfig(allow_origins=["*"]),
    )
    micro.install(app)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        client.post("/boom", headers={HEADER: "abc"})
        second = client.post("/boom", headers={HEADER: "abc"})

    # Assert
    assert second.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert "idempotent-replayed" not in second.headers
    assert calls == [1, 1]


def test_a_parameter_a_mount_and_its_route_both_name_is_served() -> None:
    """Starlette compiles a mount and its routes apart, and so does idempotency."""

    # Arrange
    async def member(request: Any) -> JSONResponse:  # noqa: ANN401
        return JSONResponse(dict(request.path_params))

    app = FastAPI()
    app.mount("/orgs/{id}", Router(routes=[Route("/members/{id}", member)]))

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)

    # Act
    with TestClient(app) as client:
        response = client.post("/charge", headers={"Idempotency-Key": "k-1"})

    # Assert
    assert response.json() == {"amount": 100}


def test_idempotency_middleware_key_function_builds_the_stored_key() -> None:
    """A `key=` function receives the scope and the client key."""
    # Arrange
    middleware = IdempotencyMiddleware(
        FastAPI(),
        idempotency=Idempotency("custom"),
        key=lambda scope, key: f"{scope['path']}\x1f{key}",
    )
    scope: Scope = {"type": "http", "method": "POST", "path": "/charge"}

    # Act
    stored = middleware._storage_key(scope, "key-1")

    # Assert
    assert stored == "/charge\x1fkey-1"


def test_idempotent_requests_key_function_reaches_the_middleware() -> None:
    """The component hands its `key=` function to the middleware it wires."""

    # Arrange
    def tenant_key(scope: Scope, key: str) -> str:
        return f"{scope['path']}\x1f{key}"

    component = IdempotentRequests(key=tenant_key)

    # Act
    _middleware, options = component.asgi_middleware()

    # Assert
    assert options["key"] is tenant_key


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda legacy: IdempotentRequests(**legacy), id="component"
        ),
        pytest.param(
            lambda legacy: IdempotencyMiddleware(
                FastAPI(), idempotency=Idempotency("custom"), **legacy
            ),
            id="middleware",
        ),
    ],
)
def test_idempotency_key_maker_is_an_unknown_argument(
    build: Callable[[dict[str, Any]], object],
) -> None:
    """`key_maker=` is no longer accepted."""
    # Arrange
    legacy: dict[str, Any] = {"key_maker": lambda _scope, key: key}

    # Act / Assert
    with pytest.raises(TypeError, match="key_maker"):
        build(legacy)


def test_idempotency_key_function_error_names_the_key_parameter() -> None:
    """A refused key names `key=` as the function that built it."""
    # Act / Assert
    with pytest.raises(
        IdempotencyKeyFunctionError, match="key= returned an empty string"
    ):
        _checked_key("", "abc")


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda key: IdempotentRequests(key=key), id="component"),
        pytest.param(
            lambda key: IdempotentRequests.from_config(
                IdempotentRequestsConfig(), key=key
            ),
            id="from_config",
        ),
        pytest.param(
            lambda key: IdempotencyMiddleware(
                FastAPI(), idempotency=Idempotency("custom"), key=key
            ),
            id="middleware",
        ),
    ],
)
@pytest.mark.parametrize("key", ["tenant:{path}", 42], ids=["string", "number"])
def test_idempotency_key_that_is_not_a_function_is_refused(
    build: Callable[[Any], object],
    key: object,
) -> None:
    """A `key=` that is not a function is refused at construction."""
    # Act / Assert
    with pytest.raises(TypeError, match=r"^key must be a function$"):
        build(key)


class _CallerCheck:
    """A route middleware refusing a request that names no caller."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if b"x-api-key" not in dict(scope["headers"]):
            refusal = JSONResponse({}, status_code=HTTP_401_UNAUTHORIZED)
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def _starlette_caller(request: Request) -> JSONResponse:
    """Answer the caller the request names."""
    return JSONResponse({"caller": request.headers.get("x-api-key")})


def _starlette_caller_app(**route: Any) -> Starlette:  # noqa: ANN401
    """Return an installed Starlette app answering the caller at `/whoami`."""
    app = Starlette(
        routes=[Route("/whoami", _starlette_caller, methods=["POST"], **route)]
    )
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)
    return app


def test_idempotent_requests_starlette_route_middleware_never_replays_across_callers() -> (
    None
):
    """Middleware on a Starlette route is a check of its own, so each caller runs."""
    # Arrange
    app = _starlette_caller_app(middleware=[Middleware(_CallerCheck)])

    # Act
    with TestClient(app) as client:
        alice = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "a"})
        bob = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "b"})
        anonymous = client.post("/whoami", headers={HEADER: "k"})

    # Assert
    assert alice.json() == {"caller": "a"}
    assert bob.json() == {"caller": "b"}
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert REPLAY_HEADER not in bob.headers
    assert REPLAY_HEADER not in anonymous.headers


def test_idempotent_requests_starlette_body_limit_route_stays_replayable() -> (
    None
):
    """A body limit on the route checks no caller, so the default key replays."""
    # Arrange
    app = _starlette_caller_app(max_body_size=MAX_BODY_SIZE)

    # Act
    with TestClient(app) as client:
        first = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "a"})
        second = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "b"})

    # Assert
    assert first.json() == second.json() == {"caller": "a"}
    assert second.headers[REPLAY_HEADER] == "true"


def _caller_guard(connection: ASGIConnection, _: BaseRouteHandler) -> None:
    """Refuse a request that names no caller."""
    if "x-api-key" not in connection.headers:
        raise NotAuthorizedException


async def _caller_of(request: LitestarRequest) -> str | None:
    """Return the caller the request names."""
    return request.headers.get("x-api-key")


def _litestar_caller_app(**handler: Any) -> Litestar:  # noqa: ANN401
    """Return an installed Litestar app answering the caller at `/whoami`."""

    @post("/whoami", status_code=200, **handler)
    async def whoami(request: LitestarRequest) -> dict[str, str | None]:
        return {"caller": request.headers.get("x-api-key")}

    app = Litestar(route_handlers=[whoami])
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)
    return app


def _litestar_dependent_app() -> Litestar:
    """Return an installed Litestar app whose handler runs a dependency."""

    @post(
        "/whoami",
        status_code=200,
        dependencies={"caller": Provide(_caller_of)},
    )
    async def whoami(
        caller: NamedDependency[str | None],
    ) -> dict[str, str | None]:
        return {"caller": caller}

    app = Litestar(route_handlers=[whoami])
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)
    return app


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: _litestar_caller_app(guards=[_caller_guard]), id="guard"
        ),
        pytest.param(_litestar_dependent_app, id="dependency"),
    ],
)
def test_idempotent_requests_litestar_handler_checks_never_replay_across_callers(
    build: Callable[[], Litestar],
) -> None:
    """A guard or a dependency runs before the handler, so each caller runs."""
    # Arrange
    app = build()

    # Act
    with LitestarTestClient(app=app) as client:
        alice = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "a"})
        bob = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "b"})

    # Assert
    assert alice.json() == {"caller": "a"}
    assert bob.json() == {"caller": "b"}
    assert REPLAY_HEADER not in bob.headers


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param({"opt": Anonymous()}, id="anonymous"),
        pytest.param({}, id="no checks"),
    ],
)
def test_idempotent_requests_litestar_handler_without_checks_stays_replayable(
    handler: dict[str, Any],
) -> None:
    """A handler declaring only `Anonymous()` runs no check, so it replays."""
    # Arrange
    app = _litestar_caller_app(**handler)

    # Act
    with LitestarTestClient(app=app) as client:
        first = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "a"})
        second = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "b"})

    # Assert
    assert first.json() == second.json() == {"caller": "a"}
    assert second.headers[REPLAY_HEADER] == "true"


def test_idempotent_requests_litestar_unused_dependency_stays_replayable() -> (
    None
):
    """A dependency the handler never asks for never runs, so it replays."""

    # Arrange
    @post("/whoami", status_code=200)
    async def whoami(request: LitestarRequest) -> dict[str, str | None]:
        return {"caller": request.headers.get("x-api-key")}

    app = Litestar(
        route_handlers=[whoami], dependencies={"caller": Provide(_caller_of)}
    )
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        first = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "a"})
        second = client.post("/whoami", headers={HEADER: "k", "X-API-Key": "b"})

    # Assert
    assert first.json() == second.json() == {"caller": "a"}
    assert second.headers[REPLAY_HEADER] == "true"


@asgi("/files", is_mount=True, copy_scope=True, guards=[_caller_guard])
async def _caller_files(
    scope: LitestarScope,
    receive: LitestarReceive,
    send: LitestarSend,
) -> None:
    """Answer the caller a request to any file names."""
    caller = dict(scope["headers"]).get(b"x-api-key", b"")
    await ASGIResponse(body=caller)(scope, receive, send)


def test_idempotent_requests_litestar_mount_never_replays_across_callers() -> (
    None
):
    """A guarded mount runs its guard on every path under it, so each caller runs."""
    # Arrange
    app = Litestar(route_handlers=[_caller_files])
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        replies = [
            client.post(path, headers={HEADER: path, "X-API-Key": caller})
            for path in ("/files/a/b", "/files")
            for caller in ("a", "b")
        ]

    # Assert
    assert [reply.text for reply in replies] == ["a", "b", "a", "b"]
    assert all(REPLAY_HEADER not in reply.headers for reply in replies)


def test_idempotent_requests_litestar_path_parameter_never_replays_across_callers() -> (
    None
):
    """A guarded route's `path` parameter spans segments, so each caller runs."""

    # Arrange
    @post("/files/{name:path}", status_code=200, guards=[_caller_guard])
    async def files(
        name: FromPath[str], request: LitestarRequest
    ) -> dict[str, str | None]:
        return {"caller": request.headers.get("x-api-key"), "name": name}

    app = Litestar(route_handlers=[files])
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        alice = client.post(
            "/files/a/b", headers={HEADER: "k", "X-API-Key": "a"}
        )
        bob = client.post("/files/a/b", headers={HEADER: "k", "X-API-Key": "b"})

    # Assert
    assert alice.json()["caller"] == "a"
    assert bob.json()["caller"] == "b"
    assert REPLAY_HEADER not in bob.headers


def test_idempotent_requests_litestar_handler_registered_later_never_replays_across_callers() -> (
    None
):
    """A guarded handler registered after the first request is read again."""
    # Arrange
    app = _litestar_caller_app()

    @post("/later", status_code=200, guards=[_caller_guard])
    async def later(request: LitestarRequest) -> dict[str, str | None]:
        return {"caller": request.headers.get("x-api-key")}

    # Act
    with LitestarTestClient(app=app) as client:
        client.post("/whoami", headers={HEADER: "first", "X-API-Key": "a"})
        app.register(later)
        alice = client.post("/later", headers={HEADER: "k", "X-API-Key": "a"})
        bob = client.post("/later", headers={HEADER: "k", "X-API-Key": "b"})

    # Assert
    assert alice.json() == {"caller": "a"}
    assert bob.json() == {"caller": "b"}
    assert REPLAY_HEADER not in bob.headers


def test_idempotent_requests_starlette_mount_middleware_never_replays_across_callers() -> (
    None
):
    """Middleware on a mount runs before the routes under it, so each caller runs."""
    # Arrange
    app = Starlette(
        routes=[
            Mount(
                "/api",
                routes=[Route("/whoami", _starlette_caller, methods=["POST"])],
                middleware=[Middleware(_CallerCheck)],
            )
        ]
    )
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)

    # Act
    with TestClient(app) as client:
        alice = client.post(
            "/api/whoami", headers={HEADER: "k", "X-API-Key": "a"}
        )
        bob = client.post(
            "/api/whoami", headers={HEADER: "k", "X-API-Key": "b"}
        )

    # Assert
    assert alice.json() == {"caller": "a"}
    assert bob.json() == {"caller": "b"}
    assert REPLAY_HEADER not in bob.headers


@pytest.mark.parametrize("framework", ["starlette", "fastapi"])
def test_idempotent_requests_mounted_app_middleware_never_replays_across_callers(
    framework: str,
) -> None:
    """Middleware a mounted app runs before its routes stops replay across callers."""
    # Arrange
    sub: Starlette = (
        Starlette(middleware=[Middleware(_CallerCheck)])
        if framework == "starlette"
        else FastAPI(openapi_url=None, middleware=[Middleware(_CallerCheck)])
    )
    sub.add_route("/whoami", _starlette_caller, methods=["POST"])
    app = Starlette(routes=[Mount("/api", app=sub)])
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)

    # Act
    with TestClient(app) as client:
        alice = client.post(
            "/api/whoami", headers={HEADER: "k", "X-API-Key": "a"}
        )
        bob = client.post(
            "/api/whoami", headers={HEADER: "k", "X-API-Key": "b"}
        )

    # Assert
    assert alice.json() == {"caller": "a"}
    assert bob.json() == {"caller": "b"}
    assert REPLAY_HEADER not in bob.headers


def test_idempotency_middleware_listing_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An integration failing to list the routes of its app fails the request."""

    # Arrange
    def broken(app: object) -> list[object]:  # noqa: ARG001
        msg = "routes moved"
        raise AttributeError(msg)

    integration = SimpleNamespace(route_declarations=broken)
    monkeypatch.setattr(
        "grelmicro.http._idempotency.load_integration",
        lambda app: integration,  # noqa: ARG005
    )
    app = _starlette_caller_app()

    # Act / Assert
    with (
        TestClient(app) as client,
        pytest.raises(AttributeError, match="routes moved"),
    ):
        client.post("/whoami", headers={HEADER: "k", "X-API-Key": "a"})


def test_idempotent_requests_starlette_router_middleware_never_replays_across_callers() -> (
    None
):
    """Middleware on a mounted router runs before its routes, so each caller runs."""
    # Arrange
    router = Router(
        routes=[Route("/whoami", _starlette_caller, methods=["POST"])],
        middleware=[Middleware(_CallerCheck)],
    )
    app = Starlette(routes=[Mount("/m", app=router)])
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)

    # Act
    with TestClient(app) as client:
        alice = client.post(
            "/m/whoami", headers={HEADER: "k", "X-API-Key": "a"}
        )
        bob = client.post("/m/whoami", headers={HEADER: "k", "X-API-Key": "b"})

    # Assert
    assert alice.json() == {"caller": "a"}
    assert bob.json() == {"caller": "b"}
    assert REPLAY_HEADER not in bob.headers


def _guarded_litestar() -> Any:  # noqa: ANN401
    """Return a Litestar app whose router guards `/r/whoami`."""

    @post("/whoami", status_code=200)
    async def whoami(request: LitestarRequest) -> dict[str, str | None]:
        return {"caller": request.headers.get("x-api-key")}

    return Litestar(
        route_handlers=[
            LitestarRouter(
                "/r", route_handlers=[whoami], guards=[_caller_guard]
            )
        ]
    )


def _starlette_over_litestar() -> tuple[Any, str]:
    """Return a Starlette app mounting a guarded Litestar app."""
    return Starlette(
        routes=[Mount("/ls", app=_guarded_litestar())]
    ), "/ls/r/whoami"


def _fastapi_over_litestar() -> tuple[Any, str]:
    """Return a FastAPI app mounting a guarded Litestar app."""
    app = FastAPI()
    app.mount("/ls", _guarded_litestar())
    return app, "/ls/r/whoami"


def _litestar_over_fastapi() -> tuple[Any, str]:
    """Return a Litestar app mounting a FastAPI app that checks its caller."""
    inner = FastAPI()

    @inner.post("/whoami/")
    async def whoami(request: FastAPIRequest) -> dict[str, str | None]:
        caller = request.headers.get("x-api-key")
        if caller is None:
            return {"caller": None}
        return {"caller": caller}

    @asgi("/fa", is_mount=True, copy_scope=True)
    async def mounted(
        scope: LitestarScope, receive: LitestarReceive, send: LitestarSend
    ) -> None:
        await inner(cast("Any", scope), cast("Any", receive), cast("Any", send))

    return Litestar(route_handlers=[mounted]), "/fa/whoami"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_starlette_over_litestar, id="starlette>litestar"),
        pytest.param(_fastapi_over_litestar, id="fastapi>litestar"),
        pytest.param(_litestar_over_fastapi, id="litestar>fastapi"),
    ],
)
def test_idempotent_requests_mounted_app_of_another_framework_never_replays_across_callers(
    build: Callable[[], tuple[Any, str]],
) -> None:
    """An app the walk cannot read checks its callers after the replay decision."""
    # Arrange
    app, path = build()
    Grelmicro(uses=[MemoryProvider(), IdempotentRequests()]).install(app)
    client = (
        LitestarTestClient(app=app)
        if isinstance(app, Litestar)
        else TestClient(app)
    )

    # Act
    with client:
        alice = client.post(path, headers={HEADER: "k", "X-API-Key": "a"})
        bob = client.post(path, headers={HEADER: "k", "X-API-Key": "b"})

    # Assert
    assert alice.json() == {"caller": "a"}
    assert bob.json() == {"caller": "b"}
    assert REPLAY_HEADER not in bob.headers


def test_idempotent_requests_cached_route_under_checked_mount_never_replays_across_callers() -> (
    None
):
    """A cached route under a checking mount installs and runs for each caller."""
    # Arrange
    api = FastAPI()

    @api.get("/feed", dependencies=[CachedResponse()])
    async def feed(request: FastAPIRequest) -> dict[str, str | None]:
        return {"caller": request.headers.get("x-api-key")}

    app = Starlette(
        routes=[Mount("/v1", app=api, middleware=[Middleware(_CallerCheck)])]
    )
    Grelmicro(
        uses=[
            MemoryProvider(),
            Cache(MemoryCacheAdapter()),
            CachedResponses(),
            IdempotentRequests(methods=["GET", "POST"]),
        ]
    ).install(app)

    # Act
    with TestClient(app) as client:
        alice = client.get("/v1/feed", headers={HEADER: "k", "X-API-Key": "a"})
        nobody = client.get("/v1/feed", headers={HEADER: "k"})

    # Assert
    assert alice.json() == {"caller": "a"}
    assert nobody.status_code == HTTP_401_UNAUTHORIZED
    assert REPLAY_HEADER not in nobody.headers
