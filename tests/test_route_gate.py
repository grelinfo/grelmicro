"""Authentication decided per route, after the framework routed the request.

Each route carries a gate built from its `RouteDeclaration`. A request with
no credential is refused before routing unless its path is excluded or the
app declares an anonymous route. An excluded request and a request whose
credential verified are routed, and the gate of the route they reach
decides, with the policy of the app serving them.
"""

from __future__ import annotations

import json
import logging
import math
import posixpath
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.convertors import (  # codespell:ignore
    Convertor,  # codespell:ignore
    register_url_convertor,
)
from starlette.endpoints import HTTPEndpoint
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.routing import (
    BaseRoute,
    Host,
    Match,
    Mount,
    Route,
    Router,
    WebSocketRoute,
)
from starlette.testclient import TestClient, WebSocketDenialResponse

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    AuthenticatedRequests,
    AuthenticatedRequestsMiddleware,
    CachedResponses,
    ErrorResponses,
    IdempotentRequests,
    RateLimitedRequests,
    RateLimitMiddleware,
    RouteDeclaration,
)
from grelmicro.http._routes import refuse_impossible
from grelmicro.integrations import starlette as starlette_integration
from grelmicro.integrations.fastapi import Anonymous
from grelmicro.integrations.starlette import (
    Authenticated,
    install_route_gate,
    route_declarations,
)
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.security import TrustedProxies
from tests.test_authentication import bearer, token, verifier

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Callable,
        Iterator,
        MutableMapping,
    )

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send
    from starlette.websockets import WebSocket

pytestmark = [pytest.mark.timeout(10)]

UNAUTHORIZED = 401
FORBIDDEN = 403
NOT_FOUND = 404
METHOD_NOT_ALLOWED = 405
REDIRECT = 307
OK = 200
SERVICE_UNAVAILABLE = 503
INTERNAL_SERVER_ERROR = 500
TOO_MANY_REQUESTS = 429
CREATED = 201
POLICY_VIOLATION = 1008
MAINTENANCE = {"x-maintenance": "on"}
EVENTS = "grelmicro.security.events"
ORIGIN = "https://app.example"
PREFLIGHT = {"origin": ORIGIN, "access-control-request-method": "GET"}
ADDRESS = ("203.0.113.7", 5000)
"""The address a test client calls from, which a rate limit can key by."""


async def served(request: Request) -> JSONResponse:
    """Answer with the path the request arrived at."""
    return JSONResponse({"path": request.url.path})


@Authenticated(scopes=["orders:write"])
async def written(request: Request) -> JSONResponse:  # noqa: ARG001
    """Answer a caller holding `orders:write`."""
    return JSONResponse({"written": True})


async def accept(websocket: WebSocket) -> None:
    """Accept the handshake and close."""
    await websocket.accept()
    await websocket.close()


@Authenticated(scopes=["chat"])
async def chat(websocket: WebSocket) -> None:
    """Accept a caller holding `chat`."""
    await websocket.accept()
    await websocket.close()


class Orders(HTTPEndpoint):
    """Reads need a caller, writes need `orders:write`."""

    async def get(self, request: Request) -> JSONResponse:  # noqa: ARG002
        """Read."""
        return JSONResponse({"read": True})

    @Authenticated(scopes=["orders:write"])
    async def post(self, request: Request) -> JSONResponse:  # noqa: ARG002
        """Write."""
        return JSONResponse({"written": True})

    async def helper(self, request: Request) -> JSONResponse:  # noqa: ARG002
        """Answer whatever method is named after it, as `HTTPEndpoint` does."""
        return JSONResponse({"helper": True})


class NormalizedPath:
    """Middleware of an app's own that resolves `..` in the path it routes."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        """Wrap `app`."""
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Route the normalized path."""
        await self.app(
            {**scope, "path": posixpath.normpath(scope["path"])}, receive, send
        )


class Maintenance:
    """Answer `503` itself, before routing, when asked to."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        """Wrap `app`."""
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Answer `503` to a request carrying `x-maintenance`, else pass it on."""
        if (b"x-maintenance", b"on") in scope["headers"]:
            await PlainTextResponse("down", status_code=503)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


class CountingCache(MemoryCacheAdapter):
    """A memory cache counting every call to it."""

    calls = 0

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401
        """Count a public method looked up."""
        found = super().__getattribute__(name)
        if callable(found) and not name.startswith("_"):
            type(self).calls += 1
        return found


async def created(request: Request) -> JSONResponse:  # noqa: ARG001
    """Answer a created order."""
    return JSONResponse({"created": True}, status_code=CREATED)


def tenant_key(scope: Scope, key: str) -> str:
    """Key an idempotent request by its caller, refusing one with none."""
    user = scope.get("user")
    if user is None or not user.is_authenticated:
        msg = "idempotency needs an authenticated caller"
        raise PermissionError(msg)
    return json.dumps(["tenant-v1", str(user.identity), scope["path"], key])


class Counted:
    """An ASGI app counting the requests it serves."""

    def __init__(self) -> None:
        """Start at zero."""
        self.calls = 0

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Count and answer."""
        self.calls += 1
        await PlainTextResponse("counted")(scope, receive, send)


def authenticated(**options: Any) -> AuthenticatedRequests:  # noqa: ANN401
    """Return the authentication every app here installs."""
    return AuthenticatedRequests(verifier(), **options)


def installed(
    app: Starlette,
    *uses: Any,  # noqa: ANN401
    errors: ErrorResponses | None = None,
    **options: Any,  # noqa: ANN401
) -> Starlette:
    """Install authentication on `app` and return it."""
    micro = Grelmicro(
        uses=[errors or ErrorResponses(), authenticated(**options), *uses]
    )
    micro.install(app)
    app.state.micro = micro
    return app


def orders_app(**options: Any) -> Starlette:  # noqa: ANN401
    """Return an app with a protected route, a scoped one and a websocket."""
    return installed(
        Starlette(
            routes=[
                Route("/orders", served),
                Route("/orders/{order_id}", written, methods=["DELETE"]),
                WebSocketRoute("/ws", accept),
                WebSocketRoute("/chat", chat),
            ]
        ),
        **options,
    )


def without_instance(content: bytes) -> dict[str, Any]:
    """Return a problem body without the path it names."""
    body = json.loads(content)
    body.pop("instance", None)
    return body


class TestRouteDeclaration:
    """What a route declares, as an integration builds it."""

    def test_path_comes_first_and_everything_else_by_keyword(self) -> None:
        """A declaration built with keywords keeps its meaning."""
        declaration = RouteDeclaration(
            "/orders", methods=frozenset({"GET"}), scopes=frozenset({"a"})
        )

        assert declaration.path == "/orders"
        assert declaration.anonymous is False
        assert declaration.cache is False
        assert declaration.own_checks is False
        with pytest.raises(TypeError):
            RouteDeclaration("/orders", None)  # type: ignore[misc]  # ty: ignore[too-many-positional-arguments]

    def test_it_is_frozen_and_hashable(self) -> None:
        """Two equal declarations are one."""
        declaration = RouteDeclaration("/orders")

        with pytest.raises(AttributeError):
            declaration.path = "/other"  # type: ignore[misc]  # ty: ignore[invalid-assignment]
        assert {declaration, RouteDeclaration("/orders")} == {declaration}

    def test_a_set_of_methods_or_scopes_is_held_frozen(self) -> None:
        """A set, a list or a tuple is taken as the frozen set it names."""
        declaration = RouteDeclaration(
            "/orders",
            methods=["GET"],  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            scopes={"a"},  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )

        assert declaration.methods == frozenset({"GET"})
        assert declaration.scopes == frozenset({"a"})

    def test_a_single_string_is_refused(self) -> None:
        """One string would read as one entry per character."""
        with pytest.raises(TypeError, match="not a single string"):
            RouteDeclaration("/orders", methods="GET")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="not a single string"):
            RouteDeclaration("/orders", scopes="orders:read")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


class TestImpossibleDeclarations:
    """A declaration that cannot hold fails where it is gated."""

    @pytest.mark.parametrize(
        ("declaration", "message"),
        [
            (
                RouteDeclaration("/a", anonymous=True, scopes=frozenset({"x"})),
                "anonymous=True and scopes",
            ),
            (
                RouteDeclaration(
                    "/a",
                    methods=frozenset({"GET"}),
                    cache=True,
                    own_checks=True,
                ),
                "cache and own_checks",
            ),
            (
                RouteDeclaration("/a", methods=frozenset({"POST"}), cache=30),
                "Only a GET or HEAD",
            ),
            (RouteDeclaration("/a", cache=True), "every method"),
            (RouteDeclaration("/a", methods=frozenset()), "no request matches"),
            (RouteDeclaration("/a", methods=frozenset({"get"})), "in capitals"),
            (
                RouteDeclaration("/a", methods=frozenset({7})),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                "in capitals",
            ),
            (
                RouteDeclaration("/a", methods=frozenset({"GET"}), cache=0),
                "keeps nothing",
            ),
            (
                RouteDeclaration("/a", methods=frozenset({"GET"}), cache=-1.5),
                "keeps nothing",
            ),
            (
                RouteDeclaration(
                    "/a", methods=frozenset({"GET"}), cache=math.nan
                ),
                "keeps nothing",
            ),
            (
                RouteDeclaration("/a", scopes=frozenset({'a"b'})),
                "not an OAuth scope token",
            ),
        ],
    )
    def test_it_is_refused_naming_the_route(
        self, declaration: RouteDeclaration, message: str
    ) -> None:
        """The refusal names the route and what cannot hold."""
        with pytest.raises(ValueError, match=message) as refused:
            refuse_impossible(declaration)

        assert "/a" in str(refused.value)

    def test_a_cache_that_is_not_a_number_is_refused(self) -> None:
        """Only a boolean or a number of seconds says how long."""
        with pytest.raises(TypeError, match="cache='soon'"):
            refuse_impossible(
                RouteDeclaration("/a", methods=frozenset({"GET"}), cache="soon")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            )

    @pytest.mark.parametrize("cache", [True, 30, 0.5])
    def test_a_cached_read_holds(self, cache: float) -> None:
        """A `GET` and its `HEAD` may be cached, for the TTL or for seconds."""
        refuse_impossible(
            RouteDeclaration(
                "/a", methods=frozenset({"GET", "HEAD"}), cache=cache
            )
        )

    def test_the_gate_refuses_it_at_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`gate(app, declaration)` raises, so a wrong route fails at install."""
        impossible = RouteDeclaration(
            "/a", methods=GET, anonymous=True, scopes=frozenset({"x"})
        )

        with pytest.raises(ValueError, match="GET /a declares anonymous"):
            plugged(monkeypatch, {"/a": (impossible,)})


def plugin(declared: dict[str, tuple[RouteDeclaration, ...]]) -> Any:  # noqa: ANN401
    """Return an integration gating each route the way the plugin docs show."""

    def install_route_gate(app: Starlette, gate: Any) -> None:  # noqa: ANN401
        for route in app.routes:
            route.app = gate(route.app, *declared[route.path])  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    def list_routes(app: Starlette) -> list[RouteDeclaration]:
        return [
            declaration
            for route in app.routes
            for declaration in declared[route.path]  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        ]

    return SimpleNamespace(
        install=starlette_integration.install,
        is_bound=starlette_integration.is_bound,
        install_error_responses=starlette_integration.install_error_responses,
        install_middleware=starlette_integration.install_middleware,
        install_route_gate=install_route_gate,
        route_declarations=list_routes,
    )


def plugged(
    monkeypatch: pytest.MonkeyPatch,
    declared: dict[str, tuple[RouteDeclaration, ...]],
    *uses: Any,  # noqa: ANN401
    **options: Any,  # noqa: ANN401
) -> Starlette:
    """Return an app with one route per declared path, installed through `plugin`."""
    integration = plugin(declared)
    monkeypatch.setattr(
        "grelmicro._app.load_integration",
        lambda app: integration,  # noqa: ARG005
    )
    app = Starlette(routes=[Route(path, Echo()) for path in declared])
    return installed(app, *uses, **options)


class Echo:
    """An ASGI app answering every method with the path and method it was sent."""

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Answer with what the request asked."""
        await JSONResponse({"path": scope["path"], "method": scope["method"]})(
            scope, receive, send
        )


GET = frozenset({"GET"})


def walking(
    monkeypatch: pytest.MonkeyPatch,
    declared: dict[str, tuple[RouteDeclaration, ...]],
    app: Starlette,
    **options: Any,  # noqa: ANN401
) -> Starlette:
    """Install `app` through an integration gating the routes of its mounted apps too."""

    def routes(app: Starlette) -> list[tuple[str, Route]]:
        found: list[tuple[str, Route]] = []
        for route in app.routes:
            if isinstance(route, Mount):
                found.extend(
                    (f"{route.path}{inner.path}", inner)
                    for inner in route.routes
                    if isinstance(inner, Route)
                )
            elif isinstance(route, Route):
                found.append((route.path, route))
        return found

    def install_route_gate(app: Starlette, gate: Any) -> None:  # noqa: ANN401
        for path, route in routes(app):
            route.app = gate(route.app, *declared[path])

    integration = SimpleNamespace(
        **{
            **vars(plugin(declared)),
            "install_route_gate": install_route_gate,
            "route_declarations": lambda app: [
                declaration
                for path, _ in routes(app)
                for declaration in declared[path]
            ],
        }
    )
    monkeypatch.setattr(
        "grelmicro._app.load_integration",
        lambda app: integration,  # noqa: ARG005
    )
    return installed(app, **options)


class Rewrite:
    """Middleware of a mounted app's own that routes `/legacy/` as `/open/`."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        """Wrap `app`."""
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Route the rewritten path."""
        path = scope["path"].replace("/legacy/", "/open/")
        await self.app({**scope, "path": path}, receive, send)


class TestGate:
    """What the app `gate(app, *declarations)` returns answers."""

    def test_it_serves_a_verified_caller_and_refuses_one_without(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The integration dispatches to what the gate returned."""
        app = plugged(
            monkeypatch,
            {"/orders": (RouteDeclaration("/orders", methods=GET),)},
        )
        client = TestClient(app)

        served_here = client.get("/orders", headers=bearer(token()))
        refused = client.get("/orders")

        assert served_here.json() == {"path": "/orders", "method": "GET"}
        assert refused.status_code == UNAUTHORIZED

    def test_a_method_no_declaration_names_needs_a_caller_and_no_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The declared method holds its scopes, any other only a caller."""
        app = plugged(
            monkeypatch,
            {
                "/orders": (
                    RouteDeclaration(
                        "/orders",
                        methods=GET,
                        scopes=frozenset({"orders:read"}),
                    ),
                )
            },
        )
        client = TestClient(app)
        caller = bearer(token())

        assert client.get("/orders", headers=caller).status_code == FORBIDDEN
        assert client.put("/orders", headers=caller).json() == {
            "path": "/orders",
            "method": "PUT",
        }
        assert client.put("/orders").status_code == UNAUTHORIZED

    @pytest.mark.parametrize(
        ("declarations", "error", "message"),
        [
            ((), TypeError, "no declaration"),
            (
                (
                    RouteDeclaration("/orders", methods=frozenset({"GET"})),
                    RouteDeclaration(
                        "/orders", methods=frozenset({"GET", "POST"})
                    ),
                ),
                ValueError,
                "GET /orders is declared twice for GET",
            ),
            (
                (
                    RouteDeclaration("/orders"),
                    RouteDeclaration("/orders", methods=frozenset({"POST"})),
                ),
                ValueError,
                "declared twice for every method",
            ),
        ],
    )
    def test_declarations_that_do_not_say_what_a_request_meets_fail_install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        declarations: tuple[RouteDeclaration, ...],
        error: type[Exception],
        message: str,
    ) -> None:
        """None at all, or two for one method, is refused where it is gated."""
        with pytest.raises(error, match=message):
            plugged(monkeypatch, {"/orders": declarations})

    def test_an_anonymous_declaration_routes_a_request_without_a_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its gate serves it, and whatever no gate answered is `401`."""
        app = plugged(
            monkeypatch,
            {
                "/public": (
                    RouteDeclaration("/public", methods=GET, anonymous=True),
                ),
                "/private": (RouteDeclaration("/private", methods=GET),),
            },
        )
        client = TestClient(app)

        public = client.get("/public")
        private = client.get("/private")
        unanswered = [
            client.request(method, path, follow_redirects=False)
            for method, path in (
                ("POST", "/public"),
                ("GET", "/nowhere"),
                ("GET", "/public/"),
            )
        ]

        assert public.json() == {"path": "/public", "method": "GET"}
        assert private.status_code == UNAUTHORIZED
        assert [response.status_code for response in unanswered] == [
            UNAUTHORIZED
        ] * 3
        assert [
            without_instance(response.content) for response in unanswered
        ] == [without_instance(private.content)] * 3

    def test_a_path_rewritten_into_exclude_after_routing_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`exclude` serves only a request excluded before routing."""
        app = walking(
            monkeypatch,
            {
                "/public": (RouteDeclaration("/public", anonymous=True),),
                "/sub/open/x": (RouteDeclaration("/sub/open/x"),),
            },
            Starlette(
                routes=[
                    Route("/public", Echo()),
                    Mount(
                        "/sub",
                        app=Starlette(
                            routes=[Route("/open/x", Echo())],
                            middleware=[Middleware(Rewrite)],
                        ),
                    ),
                ]
            ),
            exclude=("/sub/open/*",),
        )
        client = TestClient(app)

        excluded = client.get("/sub/open/x")
        rewritten = client.get("/sub/legacy/x")

        assert excluded.status_code == OK
        assert rewritten.status_code == UNAUTHORIZED

    def test_a_request_routed_on_a_copy_of_its_scope_is_admitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Middleware of a mounted app may rebuild the scope it routes."""
        app = walking(
            monkeypatch,
            {
                "/public": (RouteDeclaration("/public", anonymous=True),),
                "/sub/open/x": (
                    RouteDeclaration("/sub/open/x", anonymous=True),
                ),
            },
            Starlette(
                routes=[
                    Route("/public", Echo()),
                    Mount(
                        "/sub",
                        app=Starlette(
                            routes=[Route("/open/x", Echo())],
                            middleware=[Middleware(Rewrite)],
                        ),
                    ),
                ]
            ),
        )

        response = TestClient(app).get("/sub/legacy/x")

        assert response.json() == {"path": "/sub/open/x", "method": "GET"}

    def test_a_cors_preflight_a_mounted_app_answers_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A browser sends none of its credentials on a preflight."""
        app = walking(
            monkeypatch,
            {
                "/public": (RouteDeclaration("/public", anonymous=True),),
                "/sub/x": (RouteDeclaration("/sub/x"),),
            },
            Starlette(
                routes=[
                    Route("/public", Echo()),
                    Mount(
                        "/sub",
                        app=Starlette(
                            routes=[Route("/x", Echo())],
                            middleware=[
                                Middleware(
                                    CORSMiddleware,
                                    allow_origins=[ORIGIN],
                                    allow_methods=["*"],
                                )
                            ],
                        ),
                    ),
                ]
            ),
        )
        client = TestClient(app)

        preflight = client.options("/sub/x", headers=PREFLIGHT)
        unrouted = client.options("/nowhere", headers=PREFLIGHT)
        plain = client.options("/sub/x", headers={"origin": ORIGIN})

        assert preflight.status_code == OK
        assert preflight.headers["access-control-allow-origin"] == ORIGIN
        assert unrouted.status_code == plain.status_code == UNAUTHORIZED

    def test_a_websocket_no_gate_answered_is_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the `401` a protected route answers."""
        app = plugged(
            monkeypatch,
            {"/public": (RouteDeclaration("/public", anonymous=True),)},
        )

        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            TestClient(app).websocket_connect("/nowhere"),
        ):
            pass  # pragma: no cover

        assert denied.value.status_code == UNAUTHORIZED

    def test_an_anonymous_route_needs_the_policy_of_an_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reached from an app without authentication, it fails closed."""
        app = plugged(
            monkeypatch,
            {"/public": (RouteDeclaration("/public", anonymous=True),)},
        )
        bare = Starlette(routes=app.routes)

        assert TestClient(app).get("/public").status_code == OK
        assert TestClient(bare).get("/public").status_code == UNAUTHORIZED

    def test_a_refusal_is_recorded_with_the_declared_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The path the declaration names, when the integration names no other."""
        caplog.set_level(logging.DEBUG, logger=EVENTS)
        app = plugged(
            monkeypatch,
            {
                "/orders/7": (
                    RouteDeclaration(
                        "/orders/{order_id}", scopes=frozenset({"orders:write"})
                    ),
                )
            },
        )

        TestClient(app).get("/orders/7", headers=bearer(token()))

        assert [
            record.__dict__["http.route"]
            for record in caplog.records
            if record.name == EVENTS
        ] == ["/orders/{order_id}"]


class TestUngated:
    """A route the integration lists without a gate never starts."""

    def test_the_gate_names_the_route_it_was_not_handed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every listed route must have been gated, before any is routed."""
        integration = fake_integration(
            gated=(RouteDeclaration("/orders", methods=GET),),
            listed=(
                RouteDeclaration("/orders", methods=GET),
                RouteDeclaration("/orders", methods=frozenset({"POST"})),
            ),
        )
        monkeypatch.setattr(
            "grelmicro._app.load_integration",
            lambda app: integration,  # noqa: ARG005
        )
        app = public_catalog()

        with pytest.raises(RuntimeError, match="POST /orders carries no"):
            Grelmicro(uses=[ErrorResponses(), authenticated()]).install(app)

        assert TestClient(app).get("/catalog").json() == {"open": True}

    def test_a_route_listed_twice_needs_two_gates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A router included under two paths is gated at both."""
        integration = fake_integration(
            gated=(RouteDeclaration("/a"),),
            listed=(RouteDeclaration("/a"), RouteDeclaration("/a")),
        )
        monkeypatch.setattr(
            "grelmicro._app.load_integration",
            lambda app: integration,  # noqa: ARG005
        )

        with pytest.raises(RuntimeError, match="/a carries no"):
            Grelmicro(uses=[ErrorResponses(), authenticated()]).install(
                SimpleNamespace(user_middleware=[], state=SimpleNamespace())
            )

    def test_an_integration_listing_routes_it_cannot_gate_fails_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Declared but never wrapped, a route would be served unchecked."""
        integration = fake_integration(gates=False)
        monkeypatch.setattr(
            "grelmicro._app.load_integration",
            lambda app: integration,  # noqa: ARG005
        )

        with pytest.raises(RuntimeError, match="/listed carries no"):
            Grelmicro(uses=[ErrorResponses(), authenticated()]).install(
                SimpleNamespace(user_middleware=[], state=SimpleNamespace())
            )

    async def test_an_integration_gating_without_listing_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is listed, so nothing can be found ungated."""
        integration = fake_integration(lists=False)
        monkeypatch.setattr(
            "grelmicro._app.load_integration",
            lambda app: integration,  # noqa: ARG005
        )
        micro = Grelmicro(uses=[ErrorResponses(), authenticated()])
        app = SimpleNamespace(user_middleware=[], state=SimpleNamespace())

        micro.install(app)
        async with micro:
            pass

        assert integration.gated == [RouteDeclaration("/gated")]

    def test_an_integration_listing_no_route_leaves_the_app_ungated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With nothing wired, a request without a credential is decided first."""
        integration = fake_integration(gates=False, listed=())
        monkeypatch.setattr(
            "grelmicro._app.load_integration",
            lambda app: integration,  # noqa: ARG005
        )
        app = public_catalog()

        Grelmicro(uses=[ErrorResponses(), authenticated()]).install(app)
        client = TestClient(app)

        assert client.get("/catalog").json() == {"open": True}
        assert client.get("/nowhere").status_code == UNAUTHORIZED

    def test_an_integration_without_hooks_serves_a_public_route_nothing_rivals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decided before routing, from the routes it reads off the app."""
        monkeypatch.setattr(
            "grelmicro._app.load_integration",
            lambda app: fake_integration(gates=False, listed=()),  # noqa: ARG005
        )
        app = public_catalog()

        @app.get("/items/featured", dependencies=[Anonymous()])
        async def featured() -> dict[str, bool]:
            return {"featured": True}  # pragma: no cover

        @app.get("/items/{item_id}")
        async def item(item_id: str) -> dict[str, str]:
            return {"item": item_id}  # pragma: no cover

        @app.get("/shelf/", dependencies=[Anonymous()])
        async def shelf() -> dict[str, bool]:
            return {"shelf": True}

        Grelmicro(uses=[ErrorResponses(), authenticated()]).install(app)
        with TestClient(app) as client:
            rivalled = client.get("/items/featured")
            redirected = client.get("/shelf", follow_redirects=False)
            root = client.get("/")

            app.router.redirect_slashes = False
            reread = client.get("/catalog")

        assert rivalled.status_code == UNAUTHORIZED
        assert redirected.status_code == REDIRECT
        assert root.status_code == UNAUTHORIZED
        assert reread.json() == {"open": True}

    @pytest.mark.parametrize("lists", [False, True])
    def test_an_integration_without_gates_describes_a_rivalled_route_as_authenticated(
        self, monkeypatch: pytest.MonkeyPatch, *, lists: bool
    ) -> None:
        """The report reads the routes as the middleware reads them before routing."""

        class Week(Convertor[str]):  # codespell:ignore
            regex = "W{9}"

            def convert(self, value: str) -> str:
                return value  # pragma: no cover

            def to_string(self, value: str) -> str:
                return value  # pragma: no cover

        register_url_convertor("grelmicro_week", Week())
        for module in ("grelmicro._app", "grelmicro.http._authentication"):
            monkeypatch.setattr(
                f"{module}.load_integration",
                lambda app: fake_integration(  # noqa: ARG005
                    gates=False, lists=lists, listed=()
                ),
            )
        app = public_catalog()

        @app.get("/items/featured", dependencies=[Anonymous()])
        async def featured() -> dict[str, bool]:
            return {"featured": True}  # pragma: no cover

        @app.get("/items/{item_id}")
        async def item(item_id: str) -> dict[str, str]:
            return {"item": item_id}  # pragma: no cover

        @app.get("/tags/{tag}", dependencies=[Anonymous()])
        async def tag(tag: str) -> dict[str, str]:
            return {"tag": tag}  # pragma: no cover

        @app.get("/days/{day:grelmicro_week}", dependencies=[Anonymous()])
        async def day(day: str) -> dict[str, str]:
            return {"day": day}  # pragma: no cover

        micro = Grelmicro(uses=[ErrorResponses(), authenticated()])
        micro.install(app)
        applies = {
            row.path: row.applies for row in micro.describe(app).endpoints
        }

        assert applies["/catalog"] == ("anonymous",)
        assert applies["/tags/{tag}"] == ("anonymous",)
        assert applies["/items/featured"] == ("authenticated",)
        assert applies["/days/{day:grelmicro_week}"] == ("authenticated",)

    def test_an_integration_without_hooks_refuses_an_app_with_no_public_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before routing, whatever the URL."""
        monkeypatch.setattr(
            "grelmicro._app.load_integration",
            lambda app: fake_integration(gates=False, listed=()),  # noqa: ARG005
        )
        app = FastAPI(openapi_url=None)

        @app.get("/orders")
        async def orders() -> list[str]:
            return []  # pragma: no cover

        Grelmicro(uses=[ErrorResponses(), authenticated()]).install(app)

        assert TestClient(app).get("/orders").status_code == UNAUTHORIZED

    def test_without_authentication_no_route_is_gated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate comes from `AuthenticatedRequests`."""
        integration = fake_integration()
        monkeypatch.setattr(
            "grelmicro._app.load_integration",
            lambda app: integration,  # noqa: ARG005
        )

        Grelmicro(uses=[ErrorResponses()]).install(
            SimpleNamespace(user_middleware=[], state=SimpleNamespace())
        )

        assert integration.gated == []


def public_catalog() -> FastAPI:
    """Return an app whose one route declares `Anonymous()`, decided before routing."""
    app = FastAPI(openapi_url=None)

    @app.get("/catalog", dependencies=[Anonymous()])
    async def catalog() -> dict[str, bool]:
        return {"open": True}

    return app


def fake_integration(
    *,
    gates: bool = True,
    lists: bool = True,
    gated: tuple[RouteDeclaration, ...] = (RouteDeclaration("/gated"),),
    listed: tuple[RouteDeclaration, ...] = (RouteDeclaration("/listed"),),
) -> Any:  # noqa: ANN401
    """Return an integration module gating `gated` and listing `listed`."""
    handed: list[RouteDeclaration] = []

    def install_middleware(app: Any, components: Any) -> None:  # noqa: ANN401
        for component in components:
            middleware, options = component.asgi_middleware()
            app.user_middleware.append(Middleware(middleware, **options))
            read_routes = getattr(component, "read_routes", None)
            if read_routes is not None and isinstance(app, Starlette):
                read_routes(app)

    def install_route_gate(app: Any, gate: Any) -> None:  # noqa: ANN401, ARG001
        gate(Counted(), *gated)
        handed.extend(gated)

    module = SimpleNamespace(
        install=lambda app, micro, *, ambient=True: None,  # noqa: ARG005
        is_bound=lambda app: True,  # noqa: ARG005
        install_middleware=install_middleware,
        gated=handed,
    )
    if gates:
        module.install_route_gate = install_route_gate
    if lists:
        module.route_declarations = lambda app: list(listed)  # noqa: ARG005
    return module


class TestNoRoute:
    """A request without a credential is refused before routing, as it always was."""

    @pytest.mark.parametrize(
        ("method", "path", "served_with_a_token"),
        [
            ("GET", "/ordexx", NOT_FOUND),
            ("PUT", "/orders", METHOD_NOT_ALLOWED),
            ("GET", "/orders/", REDIRECT),
        ],
    )
    def test_it_gets_the_401_a_protected_route_gets(
        self, method: str, path: str, served_with_a_token: int
    ) -> None:
        """Same status, headers and body, so route existence does not leak."""
        client = TestClient(orders_app())

        protected = client.get("/orders")
        refused = client.request(method, path, follow_redirects=False)
        with_token = client.request(
            method, path, headers=bearer(token()), follow_redirects=False
        )

        assert refused.status_code == protected.status_code == UNAUTHORIZED
        assert without_instance(refused.content) == without_instance(
            protected.content
        )
        assert json.loads(refused.content)["instance"] == path
        assert {
            name: value
            for name, value in refused.headers.items()
            if name != "content-length"
        } == {
            name: value
            for name, value in protected.headers.items()
            if name != "content-length"
        }
        assert with_token.status_code == served_with_a_token

    def test_the_tmf_body_is_the_same_byte_for_byte(self) -> None:
        """A format carrying no path makes the two indistinguishable."""
        app = installed(
            Starlette(routes=[Route("/orders", served)]),
            errors=ErrorResponses.tmf(),
        )
        client = TestClient(app)

        protected = client.get("/orders")
        refused = client.get("/nowhere")

        assert refused.content == protected.content
        assert refused.headers == protected.headers

    def test_a_scoped_route_names_no_scope_to_a_caller_without_a_credential(
        self,
    ) -> None:
        """Its challenge is the one a URL no route answers gets."""
        client = TestClient(orders_app())

        scoped = client.delete("/orders/7")
        missing = client.delete("/orders/7", headers=bearer(token()))

        assert scoped.status_code == UNAUTHORIZED
        assert scoped.headers["www-authenticate"] == "Bearer"
        assert missing.status_code == FORBIDDEN
        assert missing.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:write"'
        )

    def test_the_rate_limit_never_counts_a_request_without_a_credential(
        self,
    ) -> None:
        """A caller from the same address is not limited by requests refused first."""
        limiter = RateLimiter.sliding_window(
            "burst", limit=3, window=60, backend=MemoryRateLimiterAdapter()
        )
        app = installed(
            Starlette(routes=[Route("/orders", served)]),
            RateLimitedRequests(
                limiter, trusted=TrustedProxies(["10.0.0.0/8"])
            ),
        )
        client = TestClient(app, client=ADDRESS)

        refused = [client.get("/orders").status_code for _ in range(5)]
        answered = client.get("/orders", headers=bearer(token()))

        assert refused == [UNAUTHORIZED] * 5
        assert answered.status_code == OK

    def test_the_response_cache_is_never_asked_without_a_credential(
        self,
    ) -> None:
        """No cache backend call for a request refused before routing."""
        backend = CountingCache()
        app = installed(
            Starlette(routes=[Route("/feed", served)]),
            Cache(backend),
            CachedResponses(include=("/feed",)),
        )
        client = TestClient(app)
        backend.calls = 0

        statuses = [client.get("/feed").status_code for _ in range(3)]

        assert statuses == [UNAUTHORIZED] * 3
        assert backend.calls == 0

    @pytest.mark.parametrize("key_maker", [None, "tenant"])
    def test_an_idempotent_write_without_a_credential_stores_nothing(
        self, key_maker: str | None
    ) -> None:
        """The key is never claimed, so the caller's retry with a token runs."""
        backend = CountingCache()
        idempotent = (
            IdempotentRequests(key_maker=tenant_key)
            if key_maker
            else IdempotentRequests()
        )
        app = installed(
            Starlette(routes=[Route("/orders", created, methods=["POST"])]),
            Cache(backend),
            idempotent,
        )
        client = TestClient(app, raise_server_exceptions=False)
        backend.calls = 0

        refused = client.post("/orders", headers={"Idempotency-Key": "k-1"})
        calls = backend.calls
        retried = client.post(
            "/orders", headers={"Idempotency-Key": "k-1", **bearer(token())}
        )

        assert refused.status_code == UNAUTHORIZED
        assert calls == 0
        assert retried.status_code == CREATED

    def test_a_mounted_app_middleware_never_runs_without_a_credential(
        self,
    ) -> None:
        """Middleware on a mount, reading the body, sees no refused request."""
        seen: list[str] = []

        class Audit:
            def __init__(self, app: Any) -> None:  # noqa: ANN401
                self.app = app

            async def __call__(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                seen.append(scope["path"])
                await self.app(scope, receive, send)

        app = installed(
            Starlette(
                routes=[
                    Mount(
                        "/admin",
                        routes=[Route("/users", served, methods=["POST"])],
                        middleware=[Middleware(Audit)],
                    ),
                    Mount(
                        "/sub",
                        app=Starlette(
                            routes=[Route("/x", served)],
                            middleware=[Middleware(Maintenance)],
                        ),
                    ),
                ]
            )
        )
        client = TestClient(app)

        refused = client.post("/admin/users", content=b"y" * 1000)
        elsewhere = client.post("/admin/nothing", content=b"z")
        maintenance = client.get("/sub/x", headers=MAINTENANCE)
        answered = client.get(
            "/sub/x", headers={**MAINTENANCE, **bearer(token())}
        )

        assert refused.status_code == elsewhere.status_code == UNAUTHORIZED
        assert maintenance.status_code == UNAUTHORIZED
        assert answered.status_code == SERVICE_UNAVAILABLE
        assert seen == []

    async def test_an_unmatched_websocket_is_closed_before_it_is_accepted(
        self,
    ) -> None:
        """A server without the denial response extension closes it with 1008."""
        app = orders_app()
        sent: list[MutableMapping[str, Any]] = []
        received = iter([{"type": "websocket.connect"}])

        async def receive() -> dict[str, Any]:
            return next(received)

        async def send(message: MutableMapping[str, Any]) -> None:
            sent.append(message)

        async with app.router.lifespan_context(app):
            await app(
                {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "path": "/nowhere",
                    "raw_path": b"/nowhere",
                    "root_path": "",
                    "query_string": b"",
                    "headers": [],
                    "scheme": "ws",
                    "server": ("testserver", 80),
                    "client": ("203.0.113.7", 5000),
                },
                receive,
                send,
            )

        assert sent == [{"type": "websocket.close", "code": POLICY_VIOLATION}]

    def test_an_unmatched_websocket_gets_the_401_where_the_server_can_send_it(
        self,
    ) -> None:
        """The denial response a protected websocket route gets."""
        client = TestClient(orders_app())

        with (
            pytest.raises(WebSocketDenialResponse) as unmatched,
            client.websocket_connect("/nowhere"),
        ):
            pass  # pragma: no cover
        with (
            pytest.raises(WebSocketDenialResponse) as protected,
            client.websocket_connect("/ws"),
        ):
            pass  # pragma: no cover

        assert unmatched.value.status_code == protected.value.status_code
        assert unmatched.value.status_code == UNAUTHORIZED


def async_client(app: Any) -> httpx.AsyncClient:  # noqa: ANN401
    """Return a client that drives `app` in the test's context, from `ADDRESS`."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=ADDRESS),
        base_url="http://testserver",
    )


def limited(limit: int = 3) -> RateLimitedRequests:
    """Return a per-caller rate limit of `limit` requests a minute."""
    return RateLimitedRequests(
        RateLimiter.sliding_window(
            "burst", limit=limit, window=60, backend=MemoryRateLimiterAdapter()
        ),
        trusted=TrustedProxies(["10.0.0.0/8"]),
    )


class TestAnsweringMiddleware:
    """Rate limits, cached responses and idempotency run at the route, after its gate."""

    def test_a_request_no_route_answers_spends_nothing(self) -> None:
        """With a token too: only a route spends the caller's bucket."""
        app = installed(Starlette(routes=[Route("/orders", served)]), limited())
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        unrouted = [
            client.get("/nowhere", headers=caller).status_code for _ in range(5)
        ]
        answered = client.get("/orders", headers=caller)

        assert unrouted == [NOT_FOUND] * 5
        assert answered.status_code == OK

    @pytest.mark.usefixtures("clock")
    async def test_under_a_mount_they_match_the_app_paths(self) -> None:
        """`include=` names the path from the app's root, as before routing."""
        app = installed(
            Starlette(routes=[Mount("/api", routes=[Route("/items", served)])]),
            RateLimitedRequests(
                RateLimiter.sliding_window(
                    "burst",
                    limit=5,
                    window=60,
                    backend=MemoryRateLimiterAdapter(),
                ),
                trusted=TrustedProxies(["10.0.0.0/8"]),
                include=("/api/*",),
            ),
        )
        async with async_client(app) as client:
            first = await client.get("/api/items", headers=bearer(token()))
            second = await client.get("/api/items", headers=bearer(token()))

        assert first.headers["ratelimit"] == '"burst";r=4;t=12'
        assert second.headers["ratelimit"] == '"burst";r=3;t=24'

    @pytest.mark.usefixtures("clock")
    async def test_an_app_installed_under_another_runs_each_app_middleware_once(
        self,
    ) -> None:
        """Each on the paths of its own app, outermost first."""

        def limit(name: str, limit: int, include: str) -> RateLimitedRequests:
            return RateLimitedRequests(
                RateLimiter.sliding_window(
                    name,
                    limit=limit,
                    window=60,
                    backend=MemoryRateLimiterAdapter(),
                ),
                trusted=TrustedProxies(["10.0.0.0/8"]),
                include=(include,),
            )

        inner = installed(
            Starlette(routes=[Route("/orders", served)]),
            limit("inner", 3, "/orders"),
        )
        outer = installed(
            Starlette(routes=[Mount("/svc", app=inner)]),
            limit("outer", 5, "/svc/*"),
        )
        async with async_client(outer) as client:
            first = await client.get("/svc/orders", headers=bearer(token()))
            second = await client.get("/svc/orders", headers=bearer(token()))

        assert first.headers["ratelimit"] == (
            '"inner";r=2;t=20, "outer";r=4;t=12'
        )
        assert second.headers["ratelimit"] == (
            '"inner";r=1;t=40, "outer";r=3;t=24'
        )

    def test_a_handler_that_raises_stores_no_idempotent_answer(self) -> None:
        """Its retry runs the handler again."""
        probe = Probe()

        async def crash(request: Request) -> JSONResponse:  # noqa: ARG001
            probe.calls += 1
            raise RuntimeError

        app = installed(
            Starlette(routes=[Route("/orders", crash, methods=["POST"])]),
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(),
        )
        headers = {"Idempotency-Key": "k-1", **bearer(token())}

        with TestClient(app, raise_server_exceptions=False) as client:
            statuses = [
                client.post("/orders", headers=headers).status_code
                for _ in range(2)
            ]

        assert statuses == [INTERNAL_SERVER_ERROR] * 2
        assert probe.calls == len(statuses)

    def test_a_public_route_that_raises_without_a_credential_is_the_servers_500(
        self,
    ) -> None:
        """The gate admitted it, so the crash is not the `401` of a URL no route answers."""
        app = FastAPI()

        @app.get("/catalog", dependencies=[Anonymous()])
        async def catalog() -> dict[str, bool]:
            raise RuntimeError

        installed(app)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/catalog")

        assert response.status_code == INTERNAL_SERVER_ERROR

    @pytest.mark.usefixtures("clock")
    async def test_an_app_mounted_in_itself_runs_them_once_at_any_depth(
        self,
    ) -> None:
        """Crossing its own authentication again adds no lane of its own."""
        app = Starlette(routes=[Route("/orders", served)])
        app.router.routes.append(Mount("/x", app=app))
        installed(app, limited(limit=100))

        async with async_client(app) as client:
            answered = [
                await client.get(
                    f"{'/x' * depth}/orders", headers=bearer(token())
                )
                for depth in (0, 1, 50)
            ]

        assert [response.status_code for response in answered] == [OK] * 3
        assert [response.headers["ratelimit"] for response in answered] == [
            '"burst";r=99;t=1',
            '"burst";r=98;t=2',
            '"burst";r=97;t=2',
        ]

    def test_what_one_of_them_raises_meets_no_handler_of_the_app(self) -> None:
        """As before routing: the server answers it, not the app's handlers."""

        class TenantUnknownError(Exception):
            pass

        def unknown_tenant(scope: Scope, key: str) -> str:  # noqa: ARG001
            raise TenantUnknownError

        async def teapot(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
            return JSONResponse({}, status_code=418)  # pragma: no cover

        app = installed(
            Starlette(
                routes=[Route("/orders", created, methods=["POST"])],
                exception_handlers={TenantUnknownError: teapot},
            ),
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(key_maker=unknown_tenant),
        )

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/orders",
                headers={"Idempotency-Key": "k-1", **bearer(token())},
            )

        assert response.status_code == INTERNAL_SERVER_ERROR

    @pytest.mark.parametrize("inner_installed", [False, True])
    def test_what_a_handler_of_a_mounted_app_raises_meets_that_app(
        self, *, inner_installed: bool
    ) -> None:
        """Its own server error handler answers it, installed or not."""

        async def crash(request: Request) -> JSONResponse:  # noqa: ARG001
            raise RuntimeError

        async def down(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
            return JSONResponse({"down": True}, status_code=SERVICE_UNAVAILABLE)

        inner = Starlette(
            routes=[Route("/orders", crash)],
            exception_handlers={Exception: down},
        )
        if inner_installed:
            installed(inner)
        outer = installed(
            Starlette(routes=[Mount("/svc", app=inner)]), limited()
        )

        with TestClient(outer, raise_server_exceptions=False) as client:
            response = client.get("/svc/orders", headers=bearer(token()))

        assert response.status_code == SERVICE_UNAVAILABLE
        assert response.json() == {"down": True}

    def test_what_one_of_them_raises_under_another_app_meets_its_edge(
        self,
    ) -> None:
        """Raised again where the app it belongs to would have raised it."""

        class TenantUnknownError(Exception):
            pass

        def unknown_tenant(scope: Scope, key: str) -> str:  # noqa: ARG001
            raise TenantUnknownError

        async def teapot(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
            return JSONResponse({}, status_code=418)  # pragma: no cover

        inner = installed(
            Starlette(
                routes=[Route("/orders", created, methods=["POST"])],
                exception_handlers={TenantUnknownError: teapot},
            )
        )
        outer = installed(
            Starlette(
                routes=[Mount("/svc", app=inner)],
                exception_handlers={TenantUnknownError: teapot},
            ),
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(key_maker=unknown_tenant),
        )

        with TestClient(outer, raise_server_exceptions=False) as client:
            response = client.post(
                "/svc/orders",
                headers={"Idempotency-Key": "k-1", **bearer(token())},
            )

        assert response.status_code == INTERNAL_SERVER_ERROR


def flooded(
    flood: int = 3,
    burst: int = 100,
    **options: Any,  # noqa: ANN401
) -> RateLimitedRequests:
    """Return a route limit beside a flood limit, a minute each."""
    return RateLimitedRequests(
        RateLimiter.sliding_window(
            "burst", limit=burst, window=60, backend=MemoryRateLimiterAdapter()
        ),
        flood=RateLimiter.sliding_window(
            "flood", limit=flood, window=60, backend=MemoryRateLimiterAdapter()
        ),
        trusted=TrustedProxies(["10.0.0.0/8"]),
        **options,
    )


class TestFloodLimit:
    """The flood limit runs before routing, with a budget of its own."""

    def test_a_flood_no_route_answers_is_refused(self) -> None:
        """With a token, once the flood budget is spent."""
        app = installed(Starlette(routes=[Route("/orders", served)]), flooded())
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        statuses = [
            client.get("/nowhere", headers=caller).status_code for _ in range(4)
        ]

        assert statuses == [NOT_FOUND] * 3 + [TOO_MANY_REQUESTS]

    def test_a_request_without_a_token_spends_nothing(self) -> None:
        """Authentication answers it `401` before the flood limit."""
        app = installed(Starlette(routes=[Route("/orders", served)]), flooded())
        client = TestClient(app, client=ADDRESS)

        refused = [client.get("/nowhere").status_code for _ in range(5)]
        unrouted = client.get("/nowhere", headers=bearer(token()))

        assert refused == [UNAUTHORIZED] * 5
        assert unrouted.status_code == NOT_FOUND

    def test_a_forged_token_spends_nothing(self) -> None:
        """Authentication refuses it before the flood limit."""
        app = installed(Starlette(routes=[Route("/orders", served)]), flooded())
        client = TestClient(app, client=ADDRESS)
        forged = {"authorization": f"Bearer {token()[:-4]}AAAA"}

        refused = [
            client.get("/nowhere", headers=forged).status_code for _ in range(5)
        ]
        unrouted = client.get("/nowhere", headers=bearer(token()))

        assert refused == [UNAUTHORIZED] * 5
        assert unrouted.status_code == NOT_FOUND

    def test_a_request_a_route_refuses_spends_the_flood_budget_only(
        self,
    ) -> None:
        """The route's bucket is left whole, the flood one is not."""
        app = installed(
            Starlette(
                routes=[
                    Route("/orders", served),
                    Route("/orders/{order_id}", written, methods=["DELETE"]),
                ]
            ),
            flooded(flood=4, burst=2),
        )
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        refused = [
            client.delete("/orders/1", headers=caller).status_code
            for _ in range(3)
        ]
        answered = client.get("/orders", headers=caller)
        turned_away = client.get("/orders", headers=caller)

        assert refused == [FORBIDDEN] * 3
        assert answered.status_code == OK
        assert answered.headers["ratelimit"].startswith('"burst";r=1;')
        assert turned_away.status_code == TOO_MANY_REQUESTS

    def test_each_answer_states_the_budget_that_metered_it(self) -> None:
        """A served request states the route's, a flood refusal the flood one."""
        app = installed(
            Starlette(routes=[Route("/orders", served)]), flooded(flood=1)
        )
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        answered = client.get("/orders", headers=caller)
        turned_away = client.get("/orders", headers=caller)

        assert answered.headers["ratelimit"].startswith('"burst";r=99;')
        assert answered.headers["ratelimit-policy"] == '"burst";q=100;w=60'
        assert turned_away.status_code == TOO_MANY_REQUESTS
        assert turned_away.headers["ratelimit"].startswith('"flood";r=0;')
        assert turned_away.headers["ratelimit-policy"] == '"flood";q=1;w=60'
        assert "retry-after" in turned_away.headers

    def test_an_excluded_path_spends_nothing(self) -> None:
        """A probe polled forever never spends a caller's flood budget."""
        app = installed(
            Starlette(
                routes=[Route("/livez", served), Route("/orders", served)]
            ),
            flooded(flood=1, exclude=("/livez",)),
        )
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        probes = [
            client.get("/livez", headers=caller).status_code for _ in range(3)
        ]
        answered = client.get("/orders", headers=caller)

        assert probes == [OK] * 3
        assert answered.status_code == OK

    def test_include_does_not_narrow_it(self) -> None:
        """A URL outside `include` still spends the flood budget."""
        app = installed(
            Starlette(routes=[Route("/orders", served)]),
            flooded(flood=1, include=("/orders",)),
        )
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        statuses = [
            client.get("/nowhere", headers=caller).status_code for _ in range(2)
        ]

        assert statuses == [NOT_FOUND, TOO_MANY_REQUESTS]

    @pytest.mark.parametrize(
        ("outer", "inner", "refused_by"), [(1, 5, "outer"), (5, 1, "inner")]
    )
    def test_an_app_installed_under_another_spends_each_flood_limit(
        self, outer: int, inner: int, refused_by: str
    ) -> None:
        """Once each, whichever is spent first."""

        def flood_of(name: str, limit: int) -> RateLimitedRequests:
            return RateLimitedRequests(
                RateLimiter.sliding_window(
                    f"{name}-burst",
                    limit=100,
                    window=60,
                    backend=MemoryRateLimiterAdapter(),
                ),
                flood=RateLimiter.sliding_window(
                    name,
                    limit=limit,
                    window=60,
                    backend=MemoryRateLimiterAdapter(),
                ),
                trusted=TrustedProxies(["10.0.0.0/8"]),
            )

        inner_app = installed(
            Starlette(routes=[Route("/orders", served)]),
            flood_of("inner", inner),
        )
        outer_app = installed(
            Starlette(routes=[Mount("/svc", app=inner_app)]),
            flood_of("outer", outer),
        )
        client = TestClient(outer_app, client=ADDRESS)
        caller = bearer(token())

        answered = client.get("/svc/orders", headers=caller)
        turned_away = client.get("/svc/orders", headers=caller)

        assert answered.status_code == OK
        assert turned_away.status_code == TOO_MANY_REQUESTS
        assert turned_away.headers["ratelimit-policy"].startswith(
            f'"{refused_by}";q=1;'
        )

    def test_a_request_with_no_caller_to_meter_spends_nothing(self) -> None:
        """A `key` returning `None` leaves it unmetered, as at the route."""
        app = installed(
            Starlette(routes=[Route("/orders", served)]),
            RateLimitedRequests(
                RateLimiter.sliding_window(
                    "burst",
                    limit=100,
                    window=60,
                    backend=MemoryRateLimiterAdapter(),
                ),
                flood=RateLimiter.sliding_window(
                    "flood",
                    limit=1,
                    window=60,
                    backend=MemoryRateLimiterAdapter(),
                ),
                key=lambda scope: None,  # noqa: ARG005
            ),
        )
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        statuses = [
            client.get("/nowhere", headers=caller).status_code for _ in range(3)
        ]

        assert statuses == [NOT_FOUND] * 3

    def test_a_websocket_handshake_spends_nothing(self) -> None:
        """As with the route limits, only HTTP requests are metered."""
        app = installed(
            Starlette(
                routes=[Route("/orders", served), WebSocketRoute("/ws", accept)]
            ),
            flooded(flood=1),
        )
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        for _ in range(3):
            with client.websocket_connect("/ws", headers=caller):
                pass
        answered = client.get("/orders", headers=caller)

        assert answered.status_code == OK

    @pytest.mark.parametrize("framework", [Starlette, FastAPI])
    def test_a_flood_limit_the_app_rate_limit_replaces_fails_install(
        self, framework: type[Starlette]
    ) -> None:
        """Before install changes anything."""
        app = framework(
            routes=[Route("/orders", served)],
            middleware=[
                Middleware(
                    RateLimitMiddleware,
                    limiters=[
                        RateLimiter.sliding_window(
                            "burst", limit=100, window=60
                        )
                    ],
                    trusted=TrustedProxies(["10.0.0.0/8"]),
                )
            ],
        )
        stack = list(app.user_middleware)

        with pytest.raises(TypeError, match="flood="):
            Grelmicro(uses=[ErrorResponses(), flooded()]).install(app)

        assert app.user_middleware == stack

    @pytest.mark.parametrize("second", [False, True])
    def test_a_failed_install_with_a_flood_limit_can_be_retried(
        self, monkeypatch: pytest.MonkeyPatch, *, second: bool
    ) -> None:
        """The middleware the first attempt added is not taken for the app's own."""
        app = Starlette(routes=[Route("/orders", served)])
        others = (
            [
                RateLimitedRequests(
                    RateLimiter.sliding_window(
                        "other",
                        limit=100,
                        window=60,
                        backend=MemoryRateLimiterAdapter(),
                    ),
                    trusted=TrustedProxies(["10.0.0.0/8"]),
                    name="other",
                )
            ]
            if second
            else []
        )
        micro = Grelmicro(
            uses=[ErrorResponses(), authenticated(), flooded(flood=1), *others]
        )

        def refused(*_: object) -> None:
            msg = "wiring refused"
            raise RuntimeError(msg)

        with monkeypatch.context() as patched:
            patched.setattr(Grelmicro, "_install_route_gate", refused)
            with pytest.raises(RuntimeError, match="wiring refused"):
                micro.install(app)
        micro.install(app)
        client = TestClient(app, client=ADDRESS)
        caller = bearer(token())

        statuses = [
            client.get("/nowhere", headers=caller).status_code for _ in range(2)
        ]

        assert statuses == [NOT_FOUND, TOO_MANY_REQUESTS]

    def test_the_app_rate_limit_stands_in_for_one_without_a_flood_limit(
        self,
    ) -> None:
        """Installed as before, with the app's own middleware kept."""
        app = Starlette(
            routes=[Route("/orders", served)],
            middleware=[
                Middleware(
                    RateLimitMiddleware,
                    limiters=[
                        RateLimiter.sliding_window(
                            "burst",
                            limit=100,
                            window=60,
                            backend=MemoryRateLimiterAdapter(),
                        )
                    ],
                    trusted=TrustedProxies(["10.0.0.0/8"]),
                )
            ],
        )
        Grelmicro(uses=[ErrorResponses(), limited()]).install(app)
        client = TestClient(app, client=ADDRESS)

        assert client.get("/orders").status_code == OK

    def test_a_flood_limit_named_as_a_route_limit_is_refused(self) -> None:
        """The two would read as one policy in the `RateLimit` fields."""
        burst = RateLimiter.sliding_window("burst", limit=100, window=60)

        with pytest.raises(ValueError, match="flood="):
            RateLimitedRequests(
                burst,
                flood=RateLimiter.sliding_window("burst", limit=600, window=60),
                trusted=TrustedProxies(["10.0.0.0/8"]),
            )

    @pytest.mark.parametrize("wiring", ["registered", "by hand"])
    def test_without_authentication_it_is_spent_first(
        self, wiring: str
    ) -> None:
        """Before routing, ahead of the limiters, registered or added by hand."""
        if wiring == "registered":
            app = Starlette(routes=[Route("/orders", served)])
            Grelmicro(
                uses=[ErrorResponses(), flooded(flood=1, burst=1)]
            ).install(app)
        else:
            app = Starlette(
                routes=[Route("/orders", served)],
                middleware=[
                    Middleware(
                        RateLimitMiddleware,
                        limiters=[
                            RateLimiter.sliding_window(
                                "burst",
                                limit=1,
                                window=60,
                                backend=MemoryRateLimiterAdapter(),
                            )
                        ],
                        flood=RateLimiter.sliding_window(
                            "flood",
                            limit=1,
                            window=60,
                            backend=MemoryRateLimiterAdapter(),
                        ),
                        trusted=TrustedProxies(["10.0.0.0/8"]),
                    )
                ],
            )
        client = TestClient(app, client=ADDRESS)

        unrouted = client.get("/nowhere")
        turned_away = client.get("/orders")

        assert unrouted.status_code == NOT_FOUND
        assert turned_away.status_code == TOO_MANY_REQUESTS
        assert turned_away.headers["ratelimit-policy"] == '"flood";q=1;w=60'


class TestAnExcludedPath:
    """A route served without a credential runs the answering middleware too."""

    def test_it_is_rate_limited(self) -> None:
        """Keyed by address, since nobody is authenticated."""
        app = installed(
            Starlette(routes=[Route("/feed", served)]),
            limited(),
            exclude=("/feed",),
        )
        client = TestClient(app, client=ADDRESS)

        statuses = [client.get("/feed").status_code for _ in range(4)]

        assert statuses == [OK, OK, OK, TOO_MANY_REQUESTS]

    def test_a_read_is_cached_on_a_miss_and_served_on_a_hit(self) -> None:
        """The handler runs once."""
        probe = Probe()
        app = installed(
            Starlette(routes=[Route("/feed", probe.endpoint)]),
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/feed": 60}),
            exclude=("/feed",),
        )

        with TestClient(app) as client:
            bodies = [client.get("/feed").json() for _ in range(2)]

        assert bodies == [{"served": True}] * 2
        assert probe.calls == 1

    def test_a_write_retried_with_its_key_is_replayed(self) -> None:
        """The handler runs once, and the retry says it was replayed."""
        probe = Probe()
        app = installed(
            Starlette(
                routes=[Route("/orders", probe.endpoint, methods=["POST"])]
            ),
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(),
            exclude=("/orders",),
        )

        with TestClient(app) as client:
            first, retried = (
                client.post("/orders", headers={"Idempotency-Key": "k-1"})
                for _ in range(2)
            )

            assert first.status_code == retried.status_code == OK
            assert retried.headers["idempotent-replayed"] == "true"
        assert probe.calls == 1

    @pytest.mark.usefixtures("clock")
    async def test_a_streamed_body_and_a_background_task_run(self) -> None:
        """Through the rate limit, which runs around them."""
        ran: list[str] = []

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            yield b"b"

        async def stream(request: Request) -> StreamingResponse:  # noqa: ARG001
            return StreamingResponse(
                chunks(), background=BackgroundTask(ran.append, "after")
            )

        app = installed(
            Starlette(routes=[Route("/stream", stream)]),
            limited(),
            exclude=("/stream",),
        )

        async with async_client(app) as client:
            response = await client.get("/stream")

        assert response.text == "ab"
        assert response.headers["ratelimit"] == '"burst";r=2;t=20'
        assert ran == ["after"]


class TestRefusedWhereItIsRouted:
    """A refusal is sent with its status, never raised into a `500`."""

    def test_a_route_in_a_mounted_app_is_refused_in_the_app_format(
        self,
    ) -> None:
        """The format registered on the app, not the mounted app's default."""
        sub = Starlette(routes=[Route("/orders", written)])
        app = installed(
            Starlette(routes=[Mount("/sub", app=sub)]),
            errors=ErrorResponses.tmf(),
        )
        client = TestClient(app, raise_server_exceptions=False)

        unauthenticated = client.get("/sub/orders")
        forbidden = client.get("/sub/orders", headers=bearer(token()))
        unrouted = client.get("/nowhere")

        assert unauthenticated.status_code == UNAUTHORIZED
        assert forbidden.status_code == FORBIDDEN
        assert unauthenticated.content == unrouted.content
        assert (
            unauthenticated.headers["content-type"]
            == forbidden.headers["content-type"]
            == unrouted.headers["content-type"]
        )

    def test_a_websocket_route_is_refused_with_its_status(self) -> None:
        """`401` without a credential, `403` for a missing scope."""
        client = TestClient(orders_app(), raise_server_exceptions=False)

        with (
            pytest.raises(WebSocketDenialResponse) as unauthenticated,
            client.websocket_connect("/chat"),
        ):
            pass  # pragma: no cover
        with (
            pytest.raises(WebSocketDenialResponse) as forbidden,
            client.websocket_connect("/chat", headers=bearer(token())),
        ):
            pass  # pragma: no cover
        with client.websocket_connect(
            "/chat", headers=bearer(token(scope="chat"))
        ):
            pass

        assert unauthenticated.value.status_code == UNAUTHORIZED
        assert forbidden.value.status_code == FORBIDDEN

    def test_the_refusal_is_recorded_with_the_route_template(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The gate knows the route it refused on."""
        caplog.set_level(logging.DEBUG, logger=EVENTS)
        client = TestClient(orders_app())

        client.delete("/orders/7")
        client.delete("/orders/7", headers=bearer(token()))

        assert [
            (record.__dict__["error.type"], record.__dict__["http.route"])
            for record in caplog.records
            if record.name == EVENTS
        ] == [
            ("authentication-required", "/orders/{order_id}"),
            ("insufficient-scope", "/orders/{order_id}"),
        ]


class TestScopesGranted:
    """The scopes a gate reads are the ones `Authenticated` reads."""

    def test_those_of_the_caller_count_when_auth_carries_none(self) -> None:
        """Middleware of the app's own may clear `scope["auth"]`."""

        class ClearAuth:
            def __init__(self, app: Any) -> None:  # noqa: ANN401
                self.app = app

            async def __call__(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                scope["auth"] = None
                await self.app(scope, receive, send)

        app = installed(
            Starlette(
                routes=[
                    Mount(
                        "/api",
                        routes=[Route("/orders", written)],
                        middleware=[Middleware(ClearAuth)],
                    )
                ]
            )
        )
        client = TestClient(app)

        granted = client.get(
            "/api/orders", headers=bearer(token(scope="orders:write"))
        )
        missing = client.get("/api/orders", headers=bearer(token()))

        assert granted.json() == {"written": True}
        assert missing.status_code == FORBIDDEN


class TestEndpointMethods:
    """An `HTTPEndpoint` declares each of its methods on its own."""

    def test_each_method_is_gated_by_its_own_scopes(self) -> None:
        """A read needs a caller, a write its scope."""
        app = installed(Starlette(routes=[Route("/orders", Orders)]))
        client = TestClient(app)
        caller = bearer(token())

        assert client.get("/orders").status_code == UNAUTHORIZED
        assert client.get("/orders", headers=caller).json() == {"read": True}
        assert client.head("/orders", headers=caller).status_code == OK
        assert client.post("/orders", headers=caller).status_code == FORBIDDEN
        assert client.post("/orders").headers["www-authenticate"] == "Bearer"
        assert client.post(
            "/orders", headers=bearer(token(scope="orders:write"))
        ).json() == {"written": True}

    def test_a_method_it_does_not_declare_needs_a_caller(self) -> None:
        """Whatever the class would answer it with."""
        app = installed(Starlette(routes=[Route("/orders", Orders)]))
        client = TestClient(app)

        assert client.put("/orders").status_code == UNAUTHORIZED
        assert client.request("HELPER", "/orders").status_code == UNAUTHORIZED
        assert (
            client.put("/orders", headers=bearer(token())).status_code
            == METHOD_NOT_ALLOWED
        )

    def test_its_declarations_are_one_per_method_set(self) -> None:
        """Methods requiring the same scopes share one declaration."""

        class Items(HTTPEndpoint):
            async def head(self, request: Request) -> JSONResponse:  # noqa: ARG002
                return JSONResponse({})  # pragma: no cover

            async def get(self, request: Request) -> JSONResponse:  # noqa: ARG002
                return JSONResponse({})  # pragma: no cover

        app = Starlette(
            routes=[
                Route("/orders", Orders),
                Route("/items", Items),
                Route("/only-post", Orders, methods=["POST"]),
            ]
        )

        assert route_declarations(app) == [
            RouteDeclaration("/orders", methods=frozenset({"GET", "HEAD"})),
            RouteDeclaration(
                "/orders",
                methods=frozenset({"POST"}),
                scopes=frozenset({"orders:write"}),
            ),
            RouteDeclaration("/items", methods=frozenset({"GET", "HEAD"})),
            RouteDeclaration(
                "/only-post",
                methods=frozenset({"POST"}),
                scopes=frozenset({"orders:write"}),
            ),
        ]

    def test_an_endpoint_answering_no_method_is_one_protected_route(
        self,
    ) -> None:
        """Listed, and gated as an authenticated route."""

        class Nothing(HTTPEndpoint):
            pass

        app = installed(Starlette(routes=[Route("/nothing", Nothing)]))

        assert route_declarations(app) == [RouteDeclaration("/nothing")]
        assert TestClient(app).get("/nothing").status_code == UNAUTHORIZED


class TestDeclarations:
    """What `route_declarations` lists, walked as the gates are."""

    def test_every_kind_of_route_is_listed(self) -> None:
        """A mount whose app is not a router is one route, and what it can read is walked."""
        counted = Counted()
        inner = Router(routes=[Route("/b", served, methods=["POST"])])
        app = Starlette(
            routes=[
                Route("/a", written),
                WebSocketRoute("/ws", chat),
                Mount("/in", routes=[Route("/x", served)]),
                Mount("/app", app=Starlette(routes=[Route("/y", served)])),
                Mount("/wrapped", app=NormalizedPath(inner)),
                Mount("/static", app=counted),
                Mount("/fastapi", app=FastAPI()),
                Host("api.example", app=Router(routes=[Route("/h", served)])),
                Host("files.example", app=counted),
                Route("/asgi", counted),
                Mount("/route", app=Route("/", served)),
            ]
        )

        assert route_declarations(app) == [
            RouteDeclaration(
                "/a",
                methods=frozenset({"GET", "HEAD"}),
                scopes=frozenset({"orders:write"}),
            ),
            RouteDeclaration("/ws", scopes=frozenset({"chat"})),
            RouteDeclaration("/in/x", methods=frozenset({"GET", "HEAD"})),
            RouteDeclaration("/app"),
            RouteDeclaration("/app/y", methods=frozenset({"GET", "HEAD"})),
            RouteDeclaration("/wrapped"),
            RouteDeclaration("/wrapped/b", methods=frozenset({"POST"})),
            RouteDeclaration("/static"),
            RouteDeclaration("/fastapi"),
            *(
                RouteDeclaration(
                    f"/fastapi{path}", methods=frozenset({"GET", "HEAD"})
                )
                for path in (
                    "/openapi.json",
                    "/docs",
                    "/docs/oauth2-redirect",
                    "/redoc",
                )
            ),
            RouteDeclaration("/h", methods=frozenset({"GET", "HEAD"})),
            RouteDeclaration("/"),
            RouteDeclaration("/asgi"),
            RouteDeclaration("/route"),
        ]

    def test_a_router_mounted_twice_is_listed_and_gated_at_both(self) -> None:
        """And the app starts, each listed route carrying its gate."""
        shared = Router(routes=[Route("/x", served)])
        app = installed(
            Starlette(routes=[Mount("/a", app=shared), Mount("/b", app=shared)])
        )

        with TestClient(app) as client:
            assert client.get("/a/x").status_code == UNAUTHORIZED
            assert client.get("/b/x", headers=bearer(token())).status_code == OK

        assert [
            declaration.path for declaration in route_declarations(app)
        ] == [
            "/a/x",
            "/b/x",
        ]

    def test_a_router_mounting_itself_is_walked_once(self) -> None:
        """A cycle ends where it started."""
        router = Router(routes=[Route("/x", served)])
        router.routes.append(Mount("/again", app=router))
        app = installed(Starlette(routes=[Mount("/r", app=router)]))

        assert [
            declaration.path for declaration in route_declarations(app)
        ] == ["/r/x"]
        assert TestClient(app).get("/r/again/x").status_code == UNAUTHORIZED

    def test_a_route_of_its_own_kind_is_one_protected_route(self) -> None:
        """Whatever it answers, a caller is needed."""

        class Probe(BaseRoute):
            path = "/probe"

            def matches(self, scope: Scope) -> tuple[Match, Scope]:
                if scope["type"] == "http" and scope["path"] == self.path:
                    return Match.FULL, {}
                return Match.NONE, {}

            async def handle(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                await PlainTextResponse("probe")(scope, receive, send)

        app = installed(Starlette(routes=[Probe()]))
        client = TestClient(app)

        assert route_declarations(app) == [RouteDeclaration("/probe")]
        assert client.get("/probe").status_code == UNAUTHORIZED
        assert client.get("/probe", headers=bearer(token())).text == "probe"


class TestOpaque:
    """What cannot be read is gated as one protected route."""

    def test_a_mounted_app_is_never_reached_without_a_credential(self) -> None:
        """Its path and everything under it."""
        counted = Counted()
        app = installed(Starlette(routes=[Mount("/static", app=counted)]))
        client = TestClient(app)

        refused = client.get("/static/file.txt")
        served_file = client.get("/static/file.txt", headers=bearer(token()))

        assert refused.status_code == UNAUTHORIZED
        assert served_file.text == "counted"
        assert counted.calls == 1

    def test_a_mount_dispatching_past_its_router_is_one_protected_route(
        self,
    ) -> None:
        """Its app sends some requests to a router the walk never sees."""
        ran: list[str] = []

        async def wipe(request: Request) -> JSONResponse:
            ran.append(request.url.path)
            return JSONResponse({"wiped": True})

        class VersionDispatch:
            def __init__(self, app: Any, v2: Any) -> None:  # noqa: ANN401
                self.app, self.v2 = app, v2

            async def __call__(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                target = (
                    self.v2
                    if (b"x-api", b"2") in scope["headers"]
                    else self.app
                )
                await target(scope, receive, send)

        v2 = Router([Route("/wipe", wipe, methods=["POST"])])
        dispatch = VersionDispatch(Router([Route("/", served)]), v2)
        app = installed(Starlette(routes=[Mount("/api", app=dispatch)]))
        client = TestClient(app)

        refused = client.post("/api/wipe", headers={"x-api": "2"})
        answered = client.post(
            "/api/wipe", headers={"x-api": "2", **bearer(token())}
        )

        assert refused.status_code == UNAUTHORIZED
        assert answered.json() == {"wiped": True}
        assert ran == ["/api/wipe"]
        assert route_declarations(app) == [
            RouteDeclaration("/api"),
            RouteDeclaration("/api/", methods=frozenset({"GET", "HEAD"})),
        ]
        assert getattr(app.router.routes[0].handle, "__self__", None) is None

    def test_a_router_default_of_its_own_is_gated(self) -> None:
        """A frontend or a proxy answering what no route matched."""
        counted = Counted()
        app = installed(Starlette(routes=[Route("/orders", served)]))
        app.router.default = counted

        client = TestClient(app)
        refused = client.post("/anything")
        answered = client.post("/anything", headers=bearer(token()))

        assert refused.status_code == UNAUTHORIZED
        assert answered.text == "counted"
        assert counted.calls == 1
        assert RouteDeclaration("/") in route_declarations(app)


class TestExclude:
    """An excluded path is held to the route it was routed to."""

    async def test_a_path_rewritten_out_of_exclude_is_refused(self) -> None:
        """The mounted app's own middleware resolved `..` on the way."""
        sub = Starlette(
            routes=[
                Route("/files/{name:path}", served),
                Route("/admin", served),
            ],
            middleware=[Middleware(NormalizedPath)],
        )
        app = installed(
            Starlette(routes=[Mount("/sub", app=sub)]),
            exclude=("/sub/files/*",),
        )

        assert await status_of(app, "/sub/files/../admin") == UNAUTHORIZED
        assert await status_of(app, "/sub/files/readme") == OK

    async def test_a_path_rewritten_into_a_fresh_scope_is_refused(
        self,
    ) -> None:
        """Middleware that rebuilds the scope drops the policy, so the gate refuses."""
        keep = (
            "type",
            "asgi",
            "method",
            "scheme",
            "path",
            "raw_path",
            "root_path",
            "query_string",
            "headers",
            "client",
            "server",
            "app",
        )

        class Legacy:
            def __init__(self, app: Any) -> None:  # noqa: ANN401
                self.app = app

            async def __call__(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                if "/legacy/" in scope["path"]:
                    fresh = {
                        name: scope[name] for name in keep if name in scope
                    }
                    fresh["path"] = scope["path"].replace("/legacy", "", 1)
                    scope = fresh
                await self.app(scope, receive, send)

        sub = Starlette(
            routes=[Route("/admin", served)], middleware=[Middleware(Legacy)]
        )
        app = installed(
            Starlette(routes=[Mount("/api", app=sub)]),
            exclude=("/api/legacy/*",),
        )

        assert await status_of(app, "/api/legacy/admin") == UNAUTHORIZED
        assert await status_of(app, "/api/admin") == UNAUTHORIZED

    @pytest.mark.parametrize("root_path", ["", "/v1"])
    def test_an_app_mounted_under_a_parent_keeps_its_exclude(
        self, root_path: str
    ) -> None:
        """Its paths read from the root it was reached at, behind a proxy too."""
        inner = installed(
            Starlette(
                routes=[
                    Route("/health", served),
                    Mount("/public", routes=[Route("/ping", served)]),
                    Route("/orders", served),
                    WebSocketRoute("/ws/public", accept),
                ]
            ),
            exclude=("/health", "/public/*", "/ws/public"),
        )
        parent = Starlette(routes=[Mount("/svc", app=inner)])

        for app, prefix in ((inner, root_path), (parent, f"{root_path}/svc")):
            with TestClient(app, root_path=root_path) as client:
                assert client.get(f"{prefix}/health").status_code == OK
                assert client.get(f"{prefix}/public/ping").status_code == OK
                assert client.get(f"{prefix}/orders").status_code == (
                    UNAUTHORIZED
                )
                with client.websocket_connect(f"{prefix}/ws/public"):
                    pass

    def test_it_is_served_under_a_root_path(self) -> None:
        """A route whose path starts like the root path included."""
        app = installed(
            Starlette(
                routes=[Route("/api/orders", served), Route("/orders", served)]
            ),
            exclude=("/api/orders",),
        )
        client = TestClient(app, root_path="/api")

        served_here = client.get("/api/api/orders")
        refused = client.get("/api/orders")

        assert served_here.status_code == OK
        assert refused.status_code == UNAUTHORIZED

    def test_a_token_sent_to_an_excluded_path_is_not_read(self) -> None:
        """Even one that would not verify."""
        app = installed(
            Starlette(routes=[Route("/livez", served)]), exclude=("/livez",)
        )

        response = TestClient(app).get("/livez", headers=bearer("forged"))

        assert response.status_code == OK

    def test_an_excluded_path_answers_its_own_404(self) -> None:
        """Nothing is authenticated there, so nothing is hidden."""
        app = installed(
            Starlette(routes=[Route("/orders", served)]),
            exclude=("/public/*",),
        )

        assert TestClient(app).get("/public/x").status_code == NOT_FOUND


async def status_of(app: Any, path: str) -> int:  # noqa: ANN401
    """Return the status `app` answers `GET path` with, as a server sends it."""
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("203.0.113.7", 5000),
        },
        receive,
        send,
    )
    return next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )


class Probe:
    """Counts the requests a handler serves."""

    def __init__(self) -> None:
        """Start at zero."""
        self.calls = 0

    async def endpoint(self, request: Request) -> JSONResponse:  # noqa: ARG002
        """Count and answer."""
        self.calls += 1
        return JSONResponse({"served": True})


@pytest.fixture
def probe() -> Probe:
    """Return a fresh probe."""
    return Probe()


def added(probe: Probe) -> Iterator[tuple[str, Callable[[Starlette], object]]]:
    """Yield each way to put `/admin` in front of the app once it runs."""
    route = Route("/admin", probe.endpoint)
    yield "add_route", lambda app: app.add_route("/admin", probe.endpoint)
    yield "append", lambda app: app.router.routes.append(route)
    yield "insert", lambda app: app.router.routes.insert(0, route)
    yield "extend", lambda app: app.router.routes.extend([route])
    yield "iadd", lambda app: iadd(app, route)
    yield (
        "slice",
        lambda app: app.router.routes.__setitem__(slice(0, 0), [route]),
    )
    yield "index", lambda app: app.router.routes.__setitem__(0, route)
    yield (
        "list",
        lambda app: setattr(app.router, "routes", [route, *app.router.routes]),
    )
    yield (
        "mount",
        lambda app: (
            app.router.routes.pop(1),
            app.mount("/admin", Starlette(routes=[Route("/", probe.endpoint)])),
        ),
    )
    yield (
        "mount app",
        lambda app: setattr(
            app.router.routes[1], "app", Router([Route("/", probe.endpoint)])
        ),
    )
    yield (
        "default",
        lambda app: (
            app.router.routes.pop(1),
            setattr(app.router, "default", Router([route]).app),
        ),
    )


def iadd(app: Starlette, route: Route) -> None:
    """Add `route` with `+=`."""
    routes = app.router.routes
    routes += [route]


def recording() -> tuple[list[str], Any]:
    """Return the paths checked, and a gate recording each request it lets through."""
    checked: list[str] = []

    def gate(
        app: Any,  # noqa: ANN401
        *declarations: RouteDeclaration,
        **options: Any,  # noqa: ANN401, ARG001
    ) -> Any:  # noqa: ANN401
        def gated(scope: Scope, receive: Receive, send: Send) -> Any:  # noqa: ANN401
            checked.append(declarations[0].path)
            return app(scope, receive, send)

        return gated

    return checked, gate


class TestRoutesAddedLater:
    """A route added after install is never served ungated."""

    @pytest.mark.parametrize("spelling", [name for name, _ in added(Probe())])
    def test_a_route_added_once_the_app_serves_is_checked_first(
        self, probe: Probe, spelling: str
    ) -> None:
        """Its gate runs before its handler, whichever way it was added."""
        checked, gate = recording()
        app = Starlette(
            routes=[Route("/orders", served), Mount("/admin", routes=[])]
        )
        install_route_gate(app, gate)
        client = TestClient(app)
        client.get("/orders")
        checked.clear()

        dict(added(probe))[spelling](app)
        answered = client.get("/admin", follow_redirects=True)

        assert answered.json() == {"served": True}
        assert probe.calls == 1
        assert checked

    def test_an_opaque_mount_given_a_router_once_the_app_serves(
        self, probe: Probe
    ) -> None:
        """The routes of its new app are gated, and the mount still is."""
        checked, gate = recording()
        mount = Mount("/static", app=Counted())
        app = Starlette(routes=[mount])
        install_route_gate(app, gate)
        client = TestClient(app)
        client.get("/static/x")
        checked.clear()

        mount.app = Router([Route("/x", probe.endpoint)])
        answered = client.get("/static/x")

        assert answered.json() == {"served": True}
        assert checked == ["/static/x"]

    def test_a_route_added_once_the_app_started_is_refused_without_a_token(
        self, probe: Probe
    ) -> None:
        """Served with one, and its handler never runs without."""
        app = installed(Starlette(routes=[Route("/orders", served)]))

        with TestClient(app) as client:
            app.add_route("/admin", probe.endpoint)
            refused = client.get("/admin")
            answered = client.get("/admin", headers=bearer(token()))

        assert refused.status_code == UNAUTHORIZED
        assert answered.json() == {"served": True}
        assert probe.calls == 1

    def test_a_scoped_route_added_after_install_is_gated_with_its_scopes(
        self,
    ) -> None:
        """Its gate refuses before its handler, naming the scopes sorted."""

        @Authenticated(scopes=["orders:write", "admin"])
        async def audit(request: Request) -> JSONResponse:  # noqa: ARG001
            return JSONResponse({})  # pragma: no cover

        app = installed(Starlette(routes=[Route("/orders", served)]))
        app.add_route("/audit", audit)

        with TestClient(app) as client:
            refused = client.get("/audit", headers=bearer(token()))

        assert refused.status_code == FORBIDDEN
        assert refused.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="admin orders:write"'
        )

    def test_a_route_list_given_back_to_its_router_stays_gated(self) -> None:
        """The same list assigned again still gates what lands in it."""
        checked, gate = recording()
        app = Starlette(routes=[Route("/orders", served)])
        install_route_gate(app, gate)
        app.router.routes = app.router.routes
        app.add_route("/admin", served)

        TestClient(app).get("/admin")

        assert checked == ["/admin"]

    def test_a_router_given_to_the_app_after_install_is_refused_at_startup(
        self, probe: Probe
    ) -> None:
        """Never served ungated: the app refuses to start."""
        app = orders_app()
        app.router = Router(routes=[Route("/admin", probe.endpoint)])

        with (
            pytest.raises(RuntimeError, match="router was replaced"),
            TestClient(app),
        ):
            pass  # pragma: no cover
        assert probe.calls == 0

    @pytest.mark.parametrize("started", [False, True])
    async def test_a_mounted_app_given_a_router_before_it_serves(
        self, *, started: bool
    ) -> None:
        """Its new routes are gated before its first request, not refused."""
        sub = Starlette(
            routes=[Route("/files/{name}", served)],
            middleware=[Middleware(NormalizedPath)],
        )
        app = installed(
            Starlette(routes=[Mount("/sub", app=sub)]),
            exclude=("/sub/files/*",),
        )
        replaced = Router(
            [Route("/files/{name}", served), Route("/admin", served)]
        )

        if started:
            async with app.router.lifespan_context(app):
                sub.router = replaced
                rewritten = await status_of(app, "/sub/files/../admin")
                excluded = await status_of(app, "/sub/files/readme")
        else:
            sub.router = replaced
            rewritten = await status_of(app, "/sub/files/../admin")
            excluded = await status_of(app, "/sub/files/readme")

        assert rewritten == UNAUTHORIZED
        assert excluded == OK

    async def test_a_mounted_app_that_served_before_install_serves_its_gated_router(
        self, probe: Probe
    ) -> None:
        """A stack it built before install never serves the router it replaced."""
        sub = Starlette(
            routes=[
                Route("/files/{name}", served),
                Route("/admin", probe.endpoint),
            ],
            middleware=[Middleware(NormalizedPath)],
        )
        await status_of(sub, "/files/readme")
        sub.router = Router([Route("/files/{name}", served)])
        app = installed(
            Starlette(routes=[Mount("/sub", app=sub)]),
            exclude=("/sub/files/*",),
        )

        rewritten = await status_of(app, "/sub/files/../admin")
        excluded = await status_of(app, "/sub/files/readme")

        assert rewritten != OK
        assert probe.calls == 0
        assert excluded == OK

    def test_a_route_list_given_to_the_router_before_startup_is_gated(
        self, probe: Probe
    ) -> None:
        """The router finds it on the first thing it serves, its startup."""
        app = orders_app()
        app.router.routes = [
            *app.router.routes,
            Route("/admin", probe.endpoint),
        ]

        with TestClient(app) as client:
            refused = client.get("/admin")
        declarations = route_declarations(app)

        assert refused.status_code == UNAUTHORIZED
        assert probe.calls == 0
        assert (
            RouteDeclaration("/admin", methods=frozenset({"GET", "HEAD"}))
            in declarations
        )

    def test_the_classes_are_the_frameworks_own(self) -> None:
        """Nothing is swapped: an app and its routers keep their classes."""
        sub = Router([Route("/x", served)])
        app = installed(
            Starlette(routes=[Mount("/m", app=sub), Host("h", app=sub)])
        )

        assert type(app) is Starlette
        assert type(app.router) is Router
        assert type(sub) is Router
        assert type(app.router.routes[0]) is Mount
        assert type(app.router.routes[1]) is Host


class TestSharedRoutes:
    """A route gated for several apps answers each by its own policy."""

    @pytest.mark.parametrize("shared", ["default", "empty endpoint"])
    def test_a_router_mounted_twice_with_nothing_declared_installs(
        self, shared: str
    ) -> None:
        """Its default, or an endpoint answering no method, is gated at both paths."""

        class Empty(HTTPEndpoint):
            pass

        counted = Counted()
        router = (
            Router([Route("/x", served)], default=counted)
            if shared == "default"
            else Router([Route("/e", Empty)])
        )
        app = installed(
            Starlette(
                routes=[Mount("/v1", app=router), Mount("/v2", app=router)]
            )
        )
        path = "/nowhere" if shared == "default" else "/e"

        with TestClient(app) as client:
            refused = [client.get(f"{mount}{path}") for mount in ("/v1", "/v2")]
            answered = client.get(f"/v2{path}", headers=bearer(token()))

        assert [response.status_code for response in refused] == [
            UNAUTHORIZED,
            UNAUTHORIZED,
        ]
        assert answered.status_code == (
            OK if shared == "default" else METHOD_NOT_ALLOWED
        )

    @pytest.mark.parametrize("mount", ["/v1", "/v2"])
    def test_a_refusal_names_the_path_the_request_came_through(
        self, caplog: pytest.LogCaptureFixture, mount: str
    ) -> None:
        """A router mounted twice records each refusal under its own mount."""
        caplog.set_level(logging.DEBUG, logger=EVENTS)
        shared = Router([Route("/orders", written, methods=["POST"])])
        static = Mount("/{tenant}/static", app=Counted())
        app = installed(
            Starlette(
                routes=[
                    Mount("/v1", app=shared),
                    Mount("/v2", app=shared),
                    Mount("/files", routes=[static]),
                ]
            )
        )
        client = TestClient(app)

        client.post(f"{mount}/orders", headers=bearer(token()))
        client.get("/files/acme/static/x", headers=bearer(token()))

        assert [
            record.__dict__["http.route"]
            for record in caplog.records
            if record.name == EVENTS
        ] == [f"{mount}/orders"]

    async def test_an_exclude_of_one_app_never_opens_the_other(self) -> None:
        """One mounted app, two apps with their own `exclude`."""
        sub = Starlette(
            routes=[
                Route("/files/{name:path}", served),
                Route("/admin", served),
            ],
            middleware=[Middleware(NormalizedPath)],
        )
        public = installed(
            Starlette(routes=[Mount("/sub", app=sub)]),
            exclude=("/sub/files/*", "/sub/admin"),
        )
        internal = installed(
            Starlette(routes=[Mount("/sub", app=sub)]),
            exclude=("/sub/files/*",),
        )

        assert await status_of(public, "/sub/files/../admin") == OK
        assert await status_of(internal, "/sub/files/../admin") == UNAUTHORIZED
        assert await status_of(internal, "/sub/files/readme") == OK

    def test_routes_built_once_serve_every_app_by_its_own_policy(
        self,
    ) -> None:
        """An app factory with module-level routes, built twice."""
        routes = [Route("/docs", served), Route("/orders", written)]
        dev = installed(Starlette(routes=routes), exclude=("/docs",))
        prod = installed(Starlette(routes=routes), errors=ErrorResponses.tmf())
        dev_client, prod_client = TestClient(dev), TestClient(prod)

        assert dev_client.get("/docs").status_code == OK
        assert prod_client.get("/docs").status_code == UNAUTHORIZED
        forbidden = prod_client.get("/orders", headers=bearer(token()))
        assert forbidden.status_code == FORBIDDEN
        assert (
            forbidden.headers["content-type"]
            == (prod_client.get("/docs").headers["content-type"])
        )
        assert (
            forbidden.headers["content-type"]
            != (
                dev_client.get("/orders", headers=bearer(token())).headers[
                    "content-type"
                ]
            )
        )

    @pytest.mark.usefixtures("clock")
    async def test_routes_built_once_run_the_middleware_of_the_app_serving_them(
        self,
    ) -> None:
        """Once per request, and only the serving app's."""
        routes = [Route("/orders", served)]
        first = installed(Starlette(routes=routes), limited())
        second = installed(Starlette(routes=routes), limited(limit=5))

        answered = []
        for app in (first, second):
            async with async_client(app) as client:
                response = await client.get("/orders", headers=bearer(token()))
            answered.append(response.headers["ratelimit"])

        assert answered == ['"burst";r=2;t=20', '"burst";r=4;t=12']

    @pytest.mark.parametrize("mounted", ["asgi app", "starlette app"])
    def test_a_mount_gated_as_a_whole_in_a_router_mounted_twice(
        self, mounted: str
    ) -> None:
        """Is gated at both paths, so the app starts."""
        counted = Counted()
        inner = (
            counted
            if mounted == "asgi app"
            else Starlette(routes=[Route("/x", counted)])
        )
        shared = Router([Mount("/static", app=inner)])
        app = installed(
            Starlette(
                routes=[Mount("/v1", app=shared), Mount("/v2", app=shared)]
            )
        )

        with TestClient(app) as client:
            refused = client.get("/v1/static/x")
            answered = client.get("/v2/static/x", headers=bearer(token()))

        assert refused.status_code == UNAUTHORIZED
        assert answered.text == "counted"

    def test_a_route_added_to_a_shared_router_is_counted_by_every_app(
        self,
    ) -> None:
        """Both apps start, each listing it with a gate."""
        shared = Router([Route("/x", served)])
        first = installed(Starlette(routes=[Mount("/a", app=shared)]))
        second = installed(Starlette(routes=[Mount("/b", app=shared)]))
        shared.add_route("/y", served)

        with TestClient(first) as one, TestClient(second) as two:
            assert one.get("/a/y", headers=bearer(token())).status_code == OK
            assert two.get("/b/y").status_code == UNAUTHORIZED

    def test_an_app_without_authentication_is_refused_a_shared_mount(
        self,
    ) -> None:
        """A mount or a host gated as a whole fails closed the same way."""
        counted = Counted()
        shared = Router(
            [Mount("/static", app=counted), Host("files.example", app=counted)]
        )
        installed(Starlette(routes=[Mount("/r", app=shared)]))
        bare = TestClient(Starlette(routes=[Mount("/r", app=shared)]))

        mounted = bare.get("/r/static/x")
        hosted = bare.get("/r/x", headers={"host": "files.example"})

        assert mounted.status_code == hosted.status_code == UNAUTHORIZED
        assert counted.calls == 0

    def test_an_app_without_authentication_is_refused_a_shared_route(
        self,
    ) -> None:
        """A gated route reached without an app's policy fails closed."""
        routes = [Route("/docs", served)]
        installed(Starlette(routes=routes))
        bare = Starlette(routes=routes)

        refused = TestClient(bare).get("/docs")

        assert refused.status_code == UNAUTHORIZED
        assert refused.headers["www-authenticate"] == "Bearer"


def test_an_app_without_this_middleware_gets_no_gate() -> None:
    """Nothing of this component routes its requests, so nothing is gated."""
    assert authenticated().route_gate(Starlette()) is None


class TestHandAdded:
    """A middleware the app added itself keeps deciding before routing."""

    def test_its_routes_are_not_gated(self) -> None:
        """Its own `exclude` decides, as it always did."""
        app = Starlette(
            routes=[Route("/livez", served), Route("/orders", served)],
            middleware=[
                Middleware(
                    AuthenticatedRequestsMiddleware,
                    verifier=verifier(),
                    exclude=("/livez",),
                )
            ],
        )
        installed(app)
        client = TestClient(app)

        assert client.get("/livez").status_code == OK
        assert client.get("/nowhere").status_code == UNAUTHORIZED
        assert not getattr(
            app.router.routes[0].handle, "__grelmicro_gated__", False
        )


def test_a_route_gate_installed_by_hand_gates_every_route() -> None:
    """For an app that never goes through `install`."""
    checked, gate = recording()
    app = Starlette(routes=[Route("/orders", served)])
    install_route_gate(app, gate)

    assert TestClient(app).get("/orders").json() == {"path": "/orders"}
    assert checked == ["/orders"]
