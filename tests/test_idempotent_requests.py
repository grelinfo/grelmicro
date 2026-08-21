"""Tests for registering the idempotency middleware through `uses=[...]`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from litestar import Litestar, post
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from grelmicro import (
    Grelmicro,
    GrelmicroMiddleware,
    MiddlewarePlacementWarning,
    Usable,
)
from grelmicro.http import IdempotencyMiddleware, IdempotentRequests
from grelmicro.http._idempotency import _checked_key
from grelmicro.idempotency import Idempotency
from grelmicro.idempotency.errors import IdempotencyKeyMakerError
from grelmicro.integrations.starlette import install_middleware
from grelmicro.providers.memory import MemoryProvider

if TYPE_CHECKING:
    from starlette.requests import Request

pytestmark = [pytest.mark.timeout(5)]

HEADER = "Idempotency-Key"
MAX_BODY_SIZE = 2048
HTTP_400_BAD_REQUEST = 400
HTTP_422_UNPROCESSABLE_CONTENT = 422
HTTP_401_UNAUTHORIZED = 401
HTTP_500_INTERNAL_SERVER_ERROR = 500
HTTP_200_OK = 200


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
    assert options["header"] == HEADER
    assert options["methods"] == ("POST",)


def test_the_component_forwards_every_middleware_option() -> None:
    """What the component takes is what the middleware is built with."""

    # Arrange
    def skip(response: Any) -> bool:  # noqa: ANN401, ARG001
        return False

    component = IdempotentRequests(
        ttl=30,
        namespace="payments",
        header="X-Idempotency-Key",
        methods=("POST", "PATCH"),
        skip=skip,
        require_key=True,
        fingerprint_body=True,
        max_body_size=MAX_BODY_SIZE,
        wait_timeout=1.0,
    )

    # Act
    middleware, options = component.asgi_middleware()

    # Assert
    assert middleware is IdempotencyMiddleware
    assert options["idempotency"].name == "payments"
    assert options["header"] == "X-Idempotency-Key"
    assert options["methods"] == ("POST", "PATCH")
    assert options["skip"] is skip
    assert options["require_key"] is True
    assert options["fingerprint_body"] is True
    assert options["max_body_size"] == MAX_BODY_SIZE
    assert options["wait_timeout"] == 1.0


def test_install_documents_the_middleware_in_the_schema() -> None:
    """The header a client has to send reaches the generated schema."""
    # Arrange
    app, _micro = _charge_app(IdempotentRequests())

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert [p["name"] for p in operation["parameters"]] == [HEADER]
    assert "409" in operation["responses"]


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
        IdempotentRequests(namespace="outer", header="X-Outer"),
        IdempotentRequests(namespace="inner", header="X-Inner", name="second"),
    )

    # Act
    app.build_middleware_stack()

    # Assert
    added = [
        middleware.kwargs.get("header", "binding")
        for middleware in app.user_middleware
    ]
    assert added == ["binding", "X-Outer", "X-Inner"]


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
    assert not isinstance(binding.app, IdempotencyMiddleware)
    assert isinstance(binding.app.app, IdempotencyMiddleware)  # ty: ignore[unresolved-attribute]


def test_litestar_never_replays_an_unhandled_exception() -> None:
    """The framework's `500` is not a response the app chose to store."""
    # Arrange
    calls: list[int] = []

    @post("/boom", status_code=200)
    async def boom() -> dict[str, int]:
        calls.append(1)
        msg = "kaboom"
        raise RuntimeError(msg)

    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = Litestar(route_handlers=[boom])
    micro.install(app)

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

    No `document_openapi`, which is the case of a middleware that has
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
    """`document_openapi` is optional, like every feature-detected hook."""
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
    assert isinstance(app.asgi_handler.app, _Marker)  # ty: ignore[unresolved-attribute]


def test_the_ttl_needs_no_pattern_object() -> None:
    """The common case sets a lifetime, not an `Idempotency`."""
    # Act
    _middleware, options = IdempotentRequests(ttl=3600).asgi_middleware()

    # Assert
    assert options["idempotency"].name == "http"
    assert options["idempotency"].config.ttl == 3600  # noqa: PLR2004


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
    """The stored response is behind whatever the app put in front of it.

    A middleware of ours that answers without calling the app must never be
    the reason a request skipped authentication. Registering the component
    after the app added its own middleware is the natural order, and it is
    the order that would break this if the placement were wrong.
    """
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider(), IdempotentRequests()])
    app = FastAPI()

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

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


def test_litestar_leaves_a_middleware_the_app_already_wired() -> None:
    """Wired at construction is the better place, and one is enough."""
    # Arrange
    from litestar.middleware import DefineMiddleware  # noqa: PLC0415

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


def test_a_key_maker_returning_a_hostile_value_is_named_not_printed() -> None:
    """What a `key_maker` returns is caller data, and reading it runs code."""

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
    with pytest.raises(IdempotencyKeyMakerError, match="expected a"):
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
