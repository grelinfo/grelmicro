"""Authentication decided per route on FastAPI, once its router dispatched the request.

Each route FastAPI dispatches to carries a gate built from what it declares:
`Anonymous()`, the scopes of every `Security` around it, and what the router
it sits in was included with. The gate runs before FastAPI reads the
request, and the answering middleware run around the route it admitted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

import pytest
from fastapi import APIRouter, Body, Depends, FastAPI, Security, params
from fastapi import WebSocket as FastAPIWebSocket
from fastapi import routing as fastapi_routing
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import (
    Mount,
    Route,
    Router,
    WebSocketRoute,
    compile_path,
)
from starlette.testclient import WebSocketDenialResponse

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    AuthenticatedRequests,
    CachedResponses,
    ErrorResponses,
    IdempotentRequests,
    RateLimitedRequests,
    RouteDeclaration,
)
from grelmicro.http._authentication import _PublicRoutes
from grelmicro.integrations.fastapi import (
    Anonymous,
    Authenticated,
    CachedResponse,
    Claims,
    CurrentPrincipal,
    CurrentToken,
    OptionalPrincipal,
    install_route_gate,
    route_declarations,
)
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.security.jwt import JWTClaims
from grelmicro.security.principal import Principal, VerifiedToken
from tests.test_authentication import bearer, needs_frontend, token, verifier
from tests.test_authentication_cases import unasked
from tests.test_route_gate import (
    ADDRESS,
    CountingCache,
    async_client,
    tenant_key,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi.routing import APIRoute
    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

pytestmark = [pytest.mark.timeout(10)]

OK = 200
CREATED = 201
ACCEPTED = 101
"""A websocket handshake the handler accepted."""
UNAUTHORIZED = 401
FORBIDDEN = 403
NOT_FOUND = 404
UNPROCESSABLE = 422
TOO_MANY_REQUESTS = 429
ORIGIN = "https://app.example"
PREFLIGHT = {"origin": ORIGIN, "access-control-request-method": "GET"}


@pytest.fixture(autouse=True)
def _no_router_emulation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a test that asks the routes read before routing."""
    monkeypatch.setattr(_PublicRoutes, "matches", unasked)


def installed(app: Any, *uses: Any, **options: Any) -> Any:  # noqa: ANN401
    """Install authentication and `uses` on `app`, and return it."""
    Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(verifier(), **options),
            *uses,
        ]
    ).install(app)
    return app


@pytest.fixture
def site(tmp_path: Any) -> str:  # noqa: ANN401
    """Return a directory holding a built frontend."""
    (tmp_path / "index.html").write_text("<p>app</p>")
    return str(tmp_path)


class Calls:
    """Counts what each handler served."""

    def __init__(self) -> None:
        """Start with none."""
        self.served: list[str] = []


class Spy:
    """Middleware of an app's own, recording each path it sees."""

    def __init__(self, app: ASGIApp, seen: list[str]) -> None:
        """Wrap `app`, recording into `seen`."""
        self.app = app
        self.seen = seen

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Record the path, then pass the request on."""
        self.seen.append(scope["path"])
        await self.app(scope, receive, send)


class TestTheBody:
    """A request a route refuses is answered before FastAPI reads its body."""

    def test_an_invalid_body_on_a_protected_route_is_refused_unread(
        self,
    ) -> None:
        """`401` without a credential, never the `422` its body would get."""
        received: list[str] = []

        class Watch:
            def __init__(self, app: ASGIApp) -> None:
                self.app = app

            async def __call__(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                async def watched() -> Any:  # noqa: ANN401
                    message = await receive()
                    received.append(message["type"])
                    return message

                await self.app(scope, watched, send)

        app = FastAPI()
        app.add_middleware(Watch)

        @app.post("/orders")
        async def create(quantity: Annotated[int, Body(embed=True)]) -> int:
            return quantity  # pragma: no cover

        @app.get("/catalog", dependencies=[Anonymous()])
        async def catalog() -> list[str]:
            return []  # pragma: no cover

        installed(app)
        client = TestClient(app)

        refused = client.post("/orders", json={"quantity": "many"})
        unread = list(received)
        invalid = client.post(
            "/orders", json={"quantity": "many"}, headers=bearer(token())
        )

        assert refused.status_code == UNAUTHORIZED
        assert unread == []
        assert invalid.status_code == UNPROCESSABLE


class TestTheCaller:
    """What a route reads of its caller answers as it did before routing decided."""

    def test_each_dependency_reads_the_verified_caller(self) -> None:
        """`CurrentPrincipal`, `Claims`, `CurrentToken` and `OptionalPrincipal`."""
        app = FastAPI()

        @app.get("/me")
        async def me(
            principal: CurrentPrincipal,
            claims: Claims,
            presented: CurrentToken,
        ) -> dict[str, Any]:
            return {
                "subject": principal.subject,
                "claims": isinstance(claims, JWTClaims),
                "token": isinstance(presented, VerifiedToken),
            }

        @app.get("/catalog", dependencies=[Anonymous()])
        async def catalog(principal: OptionalPrincipal) -> dict[str, Any]:
            return {"subject": getattr(principal, "subject", None)}

        installed(app)
        client = TestClient(app)

        mine = client.get("/me", headers=bearer(token()))
        anonymous = client.get("/catalog")
        known = client.get("/catalog", headers=bearer(token()))

        assert mine.json() == {
            "subject": "user-1",
            "claims": True,
            "token": True,
        }
        assert anonymous.json() == {"subject": None}
        assert known.json() == {"subject": "user-1"}


async def listed() -> dict[str, bool]:
    """Answer on a FastAPI route."""
    return {"listed": True}


async def secret() -> dict[str, bool]:
    """Answer on a FastAPI route that is not public."""
    return {"secret": True}


async def plain(request: Request) -> JSONResponse:
    """Answer on a Starlette route."""
    return JSONResponse({"path": request.url.path})


async def accept(websocket: FastAPIWebSocket) -> None:
    """Accept the handshake and close."""
    await websocket.accept()
    await websocket.close()


def handshake(client: TestClient, path: str, **headers: str) -> int:
    """Return `101` when the handshake was accepted, or the status that denied it."""
    try:
        with client.websocket_connect(path, headers=headers):
            return ACCEPTED
    except WebSocketDenialResponse as denied:
        return denied.status_code


class TestIncludedRouters:
    """A route is gated as each include dispatches it, with what that include adds."""

    def test_one_router_included_twice_is_public_only_where_declared(
        self,
    ) -> None:
        """The include's `Anonymous()` counts for its routes alone."""
        router = APIRouter()
        router.add_api_route("/r", listed)
        app = FastAPI()
        app.include_router(router, prefix="/a")
        app.include_router(router, prefix="/b", dependencies=[Anonymous()])
        installed(app)
        client = TestClient(app)

        assert client.get("/a/r").status_code == UNAUTHORIZED
        assert client.get("/b/r").json() == {"listed": True}
        assert client.get("/a/r", headers=bearer(token())).json() == {
            "listed": True
        }

    def test_the_scopes_of_a_nested_include_are_required(self) -> None:
        """Each include around the route adds its own."""
        inner = APIRouter()
        inner.add_api_route("/r", listed)
        middle = APIRouter()
        middle.include_router(
            inner,
            prefix="/in",
            dependencies=[Authenticated(scopes=["inner:read"])],
        )
        app = FastAPI()
        app.include_router(
            middle,
            prefix="/out",
            dependencies=[Authenticated(scopes=["outer:read"])],
        )
        installed(app)
        client = TestClient(app)

        lacking = client.get(
            "/out/in/r", headers=bearer(token(scope="inner:read"))
        )
        holding = client.get(
            "/out/in/r", headers=bearer(token(scope="inner:read outer:read"))
        )

        assert lacking.status_code == FORBIDDEN
        assert holding.json() == {"listed": True}
        assert client.get("/out/in/r").status_code == UNAUTHORIZED

    def test_the_routes_an_include_dispatches_as_copies_are_gated(self) -> None:
        """A Starlette route, a websocket route and a mount of an included router."""
        router = APIRouter()
        router.routes.append(Route("/plain", plain))
        router.routes.append(WebSocketRoute("/socket", accept))
        router.routes.append(Mount("/files", routes=[Route("/x", plain)]))
        router.add_api_websocket_route("/live", accept)
        app = FastAPI()
        app.include_router(router, prefix="/api", dependencies=[Anonymous()])
        installed(app)
        client = TestClient(app)
        caller = bearer(token())

        assert client.get("/api/plain").status_code == UNAUTHORIZED
        assert client.get("/api/files/x").status_code == UNAUTHORIZED
        assert handshake(client, "/api/socket") == UNAUTHORIZED
        assert handshake(client, "/api/live") == ACCEPTED
        assert client.get("/api/plain", headers=caller).json() == {
            "path": "/api/plain"
        }
        assert client.get("/api/files/x", headers=caller).json() == {
            "path": "/api/files/x"
        }
        assert handshake(client, "/api/socket", **caller) == ACCEPTED

    def test_every_include_is_listed(self) -> None:
        """Under its prefix, with what it adds."""
        router = APIRouter()
        router.add_api_route("/r", listed, methods=["GET"])
        app = FastAPI(openapi_url=None)
        app.include_router(router, prefix="/a")
        app.include_router(
            router,
            prefix="/b",
            dependencies=[Security(listed, scopes=[]), Anonymous()],
        )

        assert route_declarations(installed(app)) == [
            RouteDeclaration("/a/r", methods=frozenset({"GET"})),
            RouteDeclaration(
                "/b/r",
                methods=frozenset({"GET"}),
                anonymous=True,
                own_checks=True,
            ),
        ]


class TestRoutesAddedLater:
    """A route added after install is gated before any request meets it."""

    @pytest.mark.parametrize("started", [False, True])
    def test_a_route_added_to_the_app_is_gated(self, *, started: bool) -> None:
        """Before or after the app started."""
        app = installed(FastAPI(openapi_url=None))

        def add() -> None:
            app.add_api_route("/late", listed)
            app.add_api_route("/late/open", listed, dependencies=[Anonymous()])

        if not started:
            add()
        with TestClient(app) as client:
            if started:
                add()
            refused = client.get("/late")
            opened = client.get("/late/open")

        assert refused.status_code == UNAUTHORIZED
        assert opened.json() == {"listed": True}

    def test_a_route_added_to_an_included_router_is_gated(self) -> None:
        """FastAPI dispatches it through a context it builds anew."""
        router = APIRouter()
        router.add_api_route("/r", listed)
        app = FastAPI(openapi_url=None)
        app.include_router(router, prefix="/api", dependencies=[Anonymous()])
        installed(app)
        with TestClient(app) as client:
            client.get("/api/r")
            router.add_api_route("/late", listed)
            router.add_route("/plain", plain)
            opened = client.get("/api/late")
            refused = client.get("/api/plain")

        assert opened.json() == {"listed": True}
        assert refused.status_code == UNAUTHORIZED

    @pytest.mark.parametrize("change", ["removed", "moved", "nested"])
    def test_an_included_route_changed_in_place_is_served_as_it_is_now(
        self, change: str
    ) -> None:
        """FastAPI answers from the routes the router holds, not from its copy of them."""
        included = APIRouter()
        included.add_api_route("/a", secret)
        included.add_api_route("/{name}", listed, dependencies=[Anonymous()])
        outer = APIRouter()
        outer.include_router(included)
        outer.include_router(included, prefix="/again")
        app = FastAPI(openapi_url=None)
        app.include_router(
            outer if change == "nested" else included, prefix="/s"
        )
        installed(app)
        with TestClient(app) as client:
            client.get("/s/warm")
            if change == "moved":
                moved = cast("APIRoute", included.routes[0])
                moved.path = "/zz"
                moved.path_regex, moved.path_format, moved.param_convertors = (
                    compile_path("/zz")
                )
            else:
                included.routes.pop(0)
            response = client.get("/s/a")

        assert response.json() == {"listed": True}

    def test_a_router_included_later_is_gated(self) -> None:
        """With what it is included with."""
        app = installed(FastAPI(openapi_url=None))
        router = APIRouter()
        router.add_api_route("/r", listed)
        app.include_router(router, prefix="/a")
        app.include_router(router, prefix="/b", dependencies=[Anonymous()])
        client = TestClient(app)

        assert client.get("/a/r").status_code == UNAUTHORIZED
        assert client.get("/b/r").json() == {"listed": True}

    def test_an_anonymous_route_added_to_a_mounted_app_is_served(self) -> None:
        """The mount that refused at its door lets it through from then on."""
        seen: list[str] = []
        sub = FastAPI(openapi_url=None)
        sub.add_middleware(Spy, seen=seen)
        sub.add_api_route("/private", listed)
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        app.add_api_route("/public", listed, dependencies=[Anonymous()])
        installed(app)
        client = TestClient(app)

        before = client.get("/sub/open").status_code
        unseen = list(seen)
        sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        after = client.get("/sub/open")

        assert before == UNAUTHORIZED
        assert unseen == []
        assert after.json() == {"listed": True}
        assert client.get("/sub/private").status_code == UNAUTHORIZED


class TestMounts:
    """What runs under a mount before a route's gate sees a request without a credential."""

    def test_a_mounted_app_holding_no_anonymous_route_never_runs(self) -> None:
        """Its middleware, and whichever app a dispatching mount picks."""
        seen: list[str] = []
        sub = FastAPI(openapi_url=None)
        sub.add_middleware(Spy, seen=seen)
        sub.add_api_route("/users", listed)

        async def answer(scope: Scope, receive: Receive, send: Send) -> None:
            await PlainTextResponse("v1")(scope, receive, send)

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            target = sub if (b"x-v", b"2") in scope["headers"] else answer
            await target(scope, receive, send)

        app = FastAPI(openapi_url=None)
        app.mount("/admin", sub)
        app.mount("/v", dispatch)
        app.add_api_route("/public", listed, dependencies=[Anonymous()])
        installed(app)
        client = TestClient(app)

        refused = [
            client.get("/admin/users").status_code,
            client.get("/v/users", headers={"x-v": "2"}).status_code,
            client.get("/v/users").status_code,
        ]
        answered = client.get("/admin/users", headers=bearer(token()))

        assert refused == [UNAUTHORIZED] * 3
        assert answered.json() == {"listed": True}
        assert seen == ["/admin/users"]

    def test_a_mounted_app_holding_an_anonymous_route_runs_its_middleware(
        self,
    ) -> None:
        """Its routes answer at their own gates, and a URL none takes is `401`."""
        seen: list[str] = []
        sub = FastAPI(openapi_url=None)
        sub.add_middleware(Spy, seen=seen)
        sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        sub.add_api_route("/private", listed)
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        installed(app)
        client = TestClient(app)

        statuses = [
            client.get("/sub/open").status_code,
            client.get("/sub/private").status_code,
            client.get("/sub/nowhere").status_code,
        ]

        assert statuses == [OK, UNAUTHORIZED, UNAUTHORIZED]
        assert seen == ["/sub/open", "/sub/private", "/sub/nowhere"]

    @pytest.mark.parametrize("removal", ["replaced", "removed", "included"])
    def test_a_mounted_app_losing_its_anonymous_route_never_runs_again(
        self, removal: str
    ) -> None:
        """Its middleware sees no request without a credential from then on."""
        seen: list[str] = []
        router = APIRouter()
        sub = FastAPI(openapi_url=None)
        sub.add_middleware(Spy, seen=seen)
        sub.add_api_route("/private", listed)
        if removal == "included":
            router.add_api_route("/open", listed, dependencies=[Anonymous()])
            sub.include_router(router)
        else:
            sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        installed(app)
        client = TestClient(app)
        client.get("/sub/open")
        seen.clear()

        if removal == "replaced":
            sub.router.routes = sub.router.routes[:-1]
        elif removal == "removed":
            sub.router.routes.pop()
        else:
            router.routes.pop()
        statuses = [
            client.get("/sub/open").status_code,
            client.get("/sub/private").status_code,
        ]

        assert statuses == [UNAUTHORIZED] * 2
        assert seen == []

    def test_a_router_taken_out_of_a_mounted_app_is_no_longer_read(
        self,
    ) -> None:
        """Its mount stops bringing it up to date on each request."""

        class Watched(list[Any]):
            reads = 0

            def __iter__(self) -> Any:  # noqa: ANN401
                Watched.reads += 1
                return super().__iter__()

        router = APIRouter()
        router.add_api_route("/r", listed)
        router.routes = Watched(router.routes)
        sub = FastAPI(openapi_url=None)
        sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        sub.include_router(router)
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        installed(app)
        client = TestClient(app)
        client.get("/sub/open")

        sub.router.routes.pop()
        client.get("/sub/open")
        Watched.reads = 0
        client.get("/sub/open")

        assert Watched.reads == 0

    def test_a_route_added_two_includes_deep_in_a_mounted_app_is_gated(
        self,
    ) -> None:
        """The include holding it is brought up to date through the mount."""
        inner = APIRouter()
        inner.add_api_route("/open", listed, dependencies=[Anonymous()])
        outer = APIRouter()
        outer.include_router(inner, prefix="/in")
        sub = FastAPI(openapi_url=None)
        sub.include_router(outer)
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        installed(app)
        client = TestClient(app)
        client.get("/sub/in/open")

        inner.add_api_route("/late", listed)

        assert client.get("/sub/in/late").status_code == UNAUTHORIZED

    def test_a_route_added_beside_a_swapped_mount_is_served(self) -> None:
        """The router a mount under it now serves is gated on its first request."""

        def plain(request: Request) -> PlainTextResponse:  # noqa: ARG001
            return PlainTextResponse("plain")  # pragma: no cover

        sub = FastAPI(openapi_url=None)
        sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        sub.mount("/m", Router([Route("/a", plain)]))
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        installed(app)
        client = TestClient(app)
        client.get("/sub/open")

        mount = cast("Mount", sub.router.routes[-1])
        mount.app = Router([Route("/b", plain)])
        sub.add_api_route("/new", listed, dependencies=[Anonymous()])

        answered = [
            client.get(path).status_code for path in ("/sub/new", "/sub/m/b")
        ]
        assert answered == [OK, UNAUTHORIZED]

    def test_a_router_included_into_a_replaced_route_list_is_gated(
        self,
    ) -> None:
        """A route taken out beside it leaves the anonymous routes served."""
        inner = FastAPI(openapi_url=None)
        inner.add_api_route("/in", listed, dependencies=[Anonymous()])
        sub = FastAPI(openapi_url=None)
        sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        sub.add_api_route("/gone", listed)
        sub.mount("/t", inner)
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        installed(app)
        client = TestClient(app)
        client.get("/sub/open")

        inner.router.routes = list(inner.router.routes)
        late = APIRouter()
        late.add_api_route("/late", listed)
        inner.include_router(late)
        sub.router.routes.pop(1)

        answered = [
            client.get(path).status_code
            for path in ("/sub/open", "/sub/t/late")
        ]
        assert answered == [OK, UNAUTHORIZED]

    def test_an_anonymous_route_added_under_a_shut_mount_is_served(
        self,
    ) -> None:
        """It opens the mount's door on its first request."""
        inner = APIRouter()
        inner.add_api_route("/private", listed)
        outer = APIRouter()
        outer.include_router(inner, prefix="/in")
        sub = FastAPI(openapi_url=None)
        sub.include_router(outer)
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        app.add_api_route("/open", listed, dependencies=[Anonymous()])
        installed(app)
        client = TestClient(app)
        client.get("/sub/in/private")

        inner.add_api_route("/open", listed, dependencies=[Anonymous()])

        answered = [
            client.get(path).status_code
            for path in ("/sub/in/open", "/sub/in/private")
        ]
        assert answered == [OK, UNAUTHORIZED]

    def test_a_cors_preflight_its_own_middleware_answers_passes(self) -> None:
        """A browser sends none of its credentials on a preflight."""
        sub = FastAPI(openapi_url=None)
        sub.add_middleware(
            CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["*"]
        )
        sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        sub.add_api_route("/private", listed)
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        installed(app)
        client = TestClient(app)

        preflight = client.options("/sub/private", headers=PREFLIGHT)
        plain_options = client.options(
            "/sub/private", headers={"origin": ORIGIN}
        )

        assert preflight.status_code == OK
        assert preflight.headers["access-control-allow-origin"] == ORIGIN
        assert plain_options.status_code == UNAUTHORIZED

    def test_a_fastapi_app_mounted_under_starlette_is_gated_route_by_route(
        self,
    ) -> None:
        """`Anonymous()` is read under a Starlette app too."""
        sub = FastAPI(openapi_url=None)
        sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        sub.add_api_route("/private", listed)
        app = installed(Starlette(routes=[Mount("/api", app=sub)]))
        client = TestClient(app)

        assert client.get("/api/open").json() == {"listed": True}
        assert client.get("/api/private").status_code == UNAUTHORIZED
        assert client.get("/api/nowhere").status_code == UNAUTHORIZED
        assert client.get("/api/private", headers=bearer(token())).json() == {
            "listed": True
        }

    @pytest.mark.parametrize("root_path", ["", "/v1"])
    def test_an_installed_app_under_an_installed_parent_keeps_its_routes(
        self, root_path: str
    ) -> None:
        """Its own gates decide, under the parent's mount and a proxy's root path."""
        router = APIRouter()
        router.add_api_route("/open", listed, dependencies=[Anonymous()])
        inner = FastAPI(openapi_url=None)
        inner.include_router(router, prefix="/api")
        inner.add_api_route("/private", listed)
        installed(inner)
        outer = FastAPI(openapi_url=None)
        outer.mount("/svc", inner)
        outer.add_api_route("/health", listed, dependencies=[Anonymous()])
        installed(outer)
        client = TestClient(outer, root_path=root_path)

        assert client.get(f"{root_path}/svc/api/open").json() == {
            "listed": True
        }
        assert client.get(f"{root_path}/svc/private").status_code == (
            UNAUTHORIZED
        )
        assert client.get(f"{root_path}/svc/nowhere").status_code == (
            UNAUTHORIZED
        )

    @pytest.mark.parametrize("outer", [Starlette, FastAPI])
    def test_a_mounted_app_refuses_a_missing_scope_with_a_403(
        self, outer: type[Starlette]
    ) -> None:
        """Answered once, in the error format of the app that authenticates."""
        sub = FastAPI(openapi_url=None)
        sub.add_api_route(
            "/orders/{order_id}",
            listed,
            methods=["DELETE"],
            dependencies=[Authenticated(scopes=["orders:write"])],
        )
        app = installed(outer(routes=[Mount("/api", app=sub)]))

        refused = TestClient(app).delete(
            "/api/orders/7", headers=bearer(token(scope="orders:read"))
        )

        assert refused.status_code == FORBIDDEN
        assert refused.headers["content-type"] == "application/problem+json"

    def test_a_url_no_route_of_an_installed_app_answers_is_refused_by_its_parent(
        self,
    ) -> None:
        """In the parent's error format, since the parent routed it there."""
        inner = FastAPI(openapi_url=None)
        inner.add_api_route("/open", listed, dependencies=[Anonymous()])
        installed(inner)
        outer = FastAPI(openapi_url=None)
        outer.mount("/svc", inner)
        Grelmicro(
            uses=[ErrorResponses.tmf(), AuthenticatedRequests(verifier())]
        ).install(outer)

        refused = TestClient(outer).get("/svc/nowhere")

        assert refused.status_code == UNAUTHORIZED
        assert refused.json()["code"] == "GREL-AUTHENTICATION-REQUIRED"

    async def test_a_path_rewritten_into_a_fresh_scope_is_refused(self) -> None:
        """Middleware that rebuilds the scope drops the policy, so the gate refuses."""
        keep = ("type", "asgi", "method", "scheme", "path", "raw_path")
        keep += ("root_path", "query_string", "headers", "client", "server")

        class Legacy:
            def __init__(self, app: ASGIApp) -> None:
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

        sub = FastAPI(openapi_url=None)
        sub.add_middleware(Legacy)
        sub.add_api_route("/open", listed, dependencies=[Anonymous()])
        sub.add_api_route("/admin", listed)
        app = FastAPI(openapi_url=None)
        app.mount("/api", sub)
        installed(app, exclude=("/api/legacy/*",))
        client = TestClient(app)

        assert client.get("/api/open").json() == {"listed": True}
        assert client.get("/api/legacy/open").status_code == UNAUTHORIZED
        assert client.get("/api/legacy/admin").status_code == UNAUTHORIZED


def per_address(limit: int = 3) -> RateLimitedRequests:
    """Return a rate limit of `limit` requests a minute for every caller at one address."""
    return RateLimitedRequests(
        RateLimiter.sliding_window(
            "burst", limit=limit, window=60, backend=MemoryRateLimiterAdapter()
        ),
        key=lambda scope: ADDRESS[0],  # noqa: ARG005
    )


def catalog(calls: Calls, *uses: Any, **options: Any) -> FastAPI:  # noqa: ANN401
    """Return an app with a public read beside a protected write."""
    app = FastAPI(openapi_url=None)

    @app.get("/items/{item_id}", dependencies=[Anonymous()])
    async def read(item_id: int) -> dict[str, int]:
        calls.served.append("read")
        return {"item": item_id}

    @app.post("/items/{item_id}", status_code=CREATED)
    async def write(item_id: int) -> dict[str, int]:
        calls.served.append("write")
        return {"item": item_id}

    return installed(app, *uses, **options)


class TestNothingIsSpentOnARefusal:
    """A request a route refuses reaches none of the answering middleware."""

    @pytest.mark.usefixtures("clock")
    async def test_the_rate_limit_of_an_address_is_not_spent(self) -> None:
        """Callers behind one address keep their budget."""
        app = catalog(Calls(), per_address())
        async with async_client(app) as client:
            refused = [
                (await client.post("/items/7")).status_code for _ in range(5)
            ]
            unrouted = [
                (await client.get("/nowhere")).status_code for _ in range(5)
            ]
            answered = await client.post("/items/7", headers=bearer(token()))

        assert refused == unrouted == [UNAUTHORIZED] * 5
        assert answered.status_code == CREATED
        assert answered.headers["ratelimit"] == '"burst";r=2;t=20'

    def test_the_cache_is_never_asked(self) -> None:
        """No backend call for a refused or an unrouted request."""
        backend = CountingCache()
        app = catalog(
            Calls(), Cache(backend), CachedResponses(include={"/items/*": 60})
        )
        with TestClient(app) as client:
            backend.calls = 0
            statuses = [
                client.post("/items/7").status_code,
                client.get("/nowhere").status_code,
            ]

        assert statuses == [UNAUTHORIZED] * 2
        assert backend.calls == 0

    @pytest.mark.parametrize("key_maker", [None, tenant_key])
    def test_an_idempotent_write_stores_nothing(self, key_maker: Any) -> None:  # noqa: ANN401
        """A `key_maker` refusing a caller with no credential is never asked."""
        calls = Calls()
        backend = CountingCache()
        idempotent = (
            IdempotentRequests(key_maker=key_maker)
            if key_maker
            else IdempotentRequests()
        )
        app = catalog(calls, Cache(backend), idempotent)
        with TestClient(app) as client:
            backend.calls = 0
            refused = client.post(
                "/items/7", headers={"Idempotency-Key": "k-1"}
            )
            stored = backend.calls
            retried = client.post(
                "/items/7",
                headers={"Idempotency-Key": "k-1", **bearer(token())},
            )

        assert refused.status_code == UNAUTHORIZED
        assert stored == 0
        assert retried.status_code == CREATED
        assert calls.served == ["write"]


class TestAnAnonymousRoute:
    """A request without a credential it admits runs the answering middleware."""

    def test_it_is_rate_limited(self) -> None:
        """By address, since nobody is authenticated."""
        app = catalog(Calls(), per_address())
        with TestClient(app) as client:
            statuses = [client.get("/items/7").status_code for _ in range(4)]

        assert statuses == [OK, OK, OK, TOO_MANY_REQUESTS]

    def test_a_read_it_caches_is_a_miss_then_a_hit(self) -> None:
        """The handler runs once."""
        calls = Calls()
        app = FastAPI(openapi_url=None)

        @app.get("/items", dependencies=[Anonymous(), CachedResponse(ttl=60)])
        async def items() -> list[str]:
            calls.served.append("items")
            return ["a"]

        installed(app, Cache(MemoryCacheAdapter()), CachedResponses())
        with TestClient(app) as client:
            bodies = [client.get("/items").json() for _ in range(2)]

        assert bodies == [["a"]] * 2
        assert calls.served == ["items"]

    def test_a_write_retried_with_its_key_is_replayed(self) -> None:
        """The handler runs once, and the retry says it was replayed."""
        calls = Calls()
        app = FastAPI(openapi_url=None)

        @app.post("/signups", dependencies=[Anonymous()], status_code=CREATED)
        async def signup() -> dict[str, bool]:
            calls.served.append("signup")
            return {"signed": True}

        installed(app, Cache(MemoryCacheAdapter()), IdempotentRequests())
        with TestClient(app) as client:
            first, retried = (
                client.post("/signups", headers={"Idempotency-Key": "k-1"})
                for _ in range(2)
            )

            assert first.status_code == retried.status_code == CREATED
            assert retried.headers["idempotent-replayed"] == "true"
        assert calls.served == ["signup"]

    @pytest.mark.usefixtures("clock")
    async def test_a_streamed_body_and_a_background_task_run(self) -> None:
        """Through the rate limit, which runs around them."""
        ran: list[str] = []

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            yield b"b"

        app = FastAPI(openapi_url=None)

        @app.get("/stream", dependencies=[Anonymous()])
        async def stream() -> StreamingResponse:
            return StreamingResponse(
                chunks(), background=BackgroundTask(ran.append, "after")
            )

        installed(app, per_address())
        async with async_client(app) as client:
            response = await client.get("/stream")

        assert response.text == "ab"
        assert response.headers["ratelimit"] == '"burst";r=2;t=20'
        assert ran == ["after"]


def flooded(flood: int = 3, burst: int = 100) -> RateLimitedRequests:
    """Return a route limit beside a flood limit, both keyed by one address."""
    return RateLimitedRequests(
        RateLimiter.sliding_window(
            "burst", limit=burst, window=60, backend=MemoryRateLimiterAdapter()
        ),
        flood=RateLimiter.sliding_window(
            "flood", limit=flood, window=60, backend=MemoryRateLimiterAdapter()
        ),
        key=lambda scope: ADDRESS[0],  # noqa: ARG005
    )


class TestFloodLimit:
    """The flood limit runs before routing, with a budget of its own."""

    def test_a_flood_no_route_answers_is_refused(self) -> None:
        """With a token, once the flood budget is spent."""
        app = catalog(Calls(), flooded())
        with TestClient(app) as client:
            statuses = [
                client.get("/nowhere", headers=bearer(token())).status_code
                for _ in range(4)
            ]

        assert statuses == [NOT_FOUND] * 3 + [TOO_MANY_REQUESTS]

    def test_a_flood_without_a_token_is_refused(self) -> None:
        """An app with a public route routes it, so it spends the flood budget."""
        app = catalog(Calls(), flooded())
        with TestClient(app) as client:
            statuses = [client.get("/nowhere").status_code for _ in range(4)]

        assert statuses == [UNAUTHORIZED] * 3 + [TOO_MANY_REQUESTS]

    def test_a_request_a_route_refuses_spends_the_flood_budget_only(
        self,
    ) -> None:
        """The route's bucket is left whole, the flood one is not."""
        app = catalog(Calls(), flooded(flood=4, burst=2))
        with TestClient(app) as client:
            refused = [client.post("/items/7").status_code for _ in range(3)]
            answered = client.post("/items/7", headers=bearer(token()))
            turned_away = client.post("/items/7", headers=bearer(token()))

        assert refused == [UNAUTHORIZED] * 3
        assert answered.status_code == CREATED
        assert answered.headers["ratelimit"].startswith('"burst";r=1;')
        assert turned_away.status_code == TOO_MANY_REQUESTS

    def test_each_answer_states_the_budget_that_metered_it(self) -> None:
        """A served request states the route's, a flood refusal the flood one."""
        app = catalog(Calls(), flooded(flood=1))
        with TestClient(app) as client:
            answered = client.get("/items/7")
            turned_away = client.get("/items/7")

        assert answered.headers["ratelimit-policy"] == '"burst";q=100;w=60'
        assert turned_away.status_code == TOO_MANY_REQUESTS
        assert turned_away.headers["ratelimit-policy"] == '"flood";q=1;w=60'


orders = APIRouter()
"""A router built once, as an app factory shares it between the apps it builds."""


@orders.get("/orders")
async def list_orders() -> list[str]:
    """Answer on a route every app shares."""
    return []


@orders.get("/catalog", dependencies=[Anonymous()])
async def list_catalog() -> list[str]:
    """Answer on a public route every app shares."""
    return []


def build(*uses: Any, **options: Any) -> FastAPI:  # noqa: ANN401
    """Return a new app serving the shared router, as an app factory does."""
    app = FastAPI(openapi_url=None)
    app.include_router(orders)
    return installed(app, *uses, **options)


class TestSharedRoutes:
    """Routes built once answer each app by its own policy."""

    @pytest.mark.usefixtures("clock")
    async def test_each_app_runs_its_own_middleware_on_them(self) -> None:
        """Once per request, and only the serving app's."""
        first = build(per_address())
        second = build(per_address(limit=5), exclude=("/orders",))

        async with async_client(first) as client:
            refused = await client.get("/orders")
            answered = await client.get("/catalog")
        async with async_client(second) as client:
            excluded = await client.get("/orders")

        assert refused.status_code == UNAUTHORIZED
        assert answered.headers["ratelimit"] == '"burst";r=2;t=20'
        assert excluded.headers["ratelimit"] == '"burst";r=4;t=12'

    def test_an_app_without_authentication_is_refused_them(self) -> None:
        """A gated route reached without the policy of an app fails closed."""
        build()
        bare = FastAPI(openapi_url=None)
        bare.include_router(orders)
        client = TestClient(bare)

        assert client.get("/catalog").status_code == UNAUTHORIZED
        assert client.get("/orders").status_code == UNAUTHORIZED

    def test_a_route_its_router_caches_is_refused_by_an_app_without_authentication(
        self,
    ) -> None:
        """Read with what its router declares, where the route is first met."""
        router = APIRouter(dependencies=[CachedResponse(ttl=60)])
        router.add_api_route(
            "/paged", listed, dependencies=[Anonymous(), Depends(page)]
        )
        app = FastAPI(openapi_url=None)
        app.include_router(router)
        installed(app, Cache(MemoryCacheAdapter()), CachedResponses())
        bare = FastAPI(openapi_url=None)
        bare.include_router(router, prefix="/b")

        assert TestClient(app).get("/paged").json() == {"listed": True}
        assert TestClient(bare).get("/b/paged").status_code == UNAUTHORIZED


class TestRefusals:
    """A refusal names the route it was refused on."""

    def test_an_included_route_is_named_under_its_prefix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """And under the root path the request arrived at."""
        caplog.set_level("DEBUG", logger="grelmicro.security.events")
        router = APIRouter()
        router.add_api_route(
            "/orders/{order_id}",
            listed,
            methods=["DELETE"],
            dependencies=[Authenticated(scopes=["orders:write"])],
        )
        app = FastAPI(openapi_url=None)
        app.include_router(router, prefix="/v1")
        installed(app)
        client = TestClient(app, root_path="/svc")

        client.delete("/svc/v1/orders/7", headers=bearer(token()))

        assert [
            record.__dict__["http.route"]
            for record in caplog.records
            if record.name == "grelmicro.security.events"
        ] == ["/svc/v1/orders/{order_id}"]


@pytest.mark.parametrize(
    ("owner", "part"),
    [
        ("_IncludedRouter", "effective_candidates"),
        pytest.param(
            "_IncludedRouter",
            "effective_low_priority_routes",
            marks=needs_frontend,
        ),
        ("_IncludedRouter", "original_router"),
        ("_EffectiveRouteContext", "original_route"),
        ("_EffectiveRouteContext", "starlette_route"),
        ("_EffectiveRouteContext", "path"),
        pytest.param(
            "_EffectiveRouteContext", "frontend_prefix", marks=needs_frontend
        ),
        ("_EffectiveRouteContext", "dependant"),  # codespell:ignore
        ("_EffectiveRouteContext", "methods"),
        ("APIRouter", "_mark_routes_changed"),
    ],
)
def test_a_fastapi_router_without_a_part_the_gates_read_fails_install(
    monkeypatch: pytest.MonkeyPatch, owner: str, part: str
) -> None:
    """A FastAPI release that moved how it dispatches includes stops `install` loudly."""
    kind = getattr(fastapi_routing, owner)
    fields = getattr(kind, "__dataclass_fields__", {})
    if part in fields:
        monkeypatch.delitem(fields, part)
    else:
        monkeypatch.delattr(kind, part)

    with pytest.raises(RuntimeError, match=rf"has no {owner}.{part}\b"):
        installed(FastAPI(openapi_url=None))


@needs_frontend
def test_a_fastapi_router_without_its_low_priority_routes_fails_install() -> (
    None
):
    """The routes it matches after every other, its frontend among them."""
    app = FastAPI(openapi_url=None)
    del app.router._low_priority_routes

    with pytest.raises(
        RuntimeError, match=r"has no APIRouter._low_priority_routes\b"
    ):
        installed(app)


@needs_frontend
class TestFrontendRoutes:
    """What `frontend()` serves after every other route is gated too."""

    def test_it_needs_a_caller_unless_declared_anonymous(
        self, site: str
    ) -> None:
        """The app's own, and one an included router declares public."""
        public = APIRouter(dependencies=[Anonymous()])
        public.frontend("/", directory=site)
        app = FastAPI(openapi_url=None)
        app.frontend("/admin", directory=site)
        app.include_router(public, prefix="/site")
        installed(app)
        client = TestClient(app)

        assert client.get("/admin/index.html").status_code == UNAUTHORIZED
        assert (
            client.get("/admin/index.html", headers=bearer(token())).text
            == "<p>app</p>"
        )
        assert client.get("/site/index.html").text == "<p>app</p>"

    def test_one_added_later_is_gated(self, site: str) -> None:
        """As it lands after every other route."""
        app = installed(FastAPI(openapi_url=None))
        app.frontend("/", directory=site)
        client = TestClient(app)

        assert client.get("/index.html").status_code == UNAUTHORIZED
        assert client.get("/index.html", headers=bearer(token())).text == (
            "<p>app</p>"
        )


class Recording:
    """A gate recording what each call declares, letting every request through."""

    def __init__(self) -> None:
        """Start with no call."""
        self.calls: list[tuple[tuple[RouteDeclaration, ...], bool]] = []
        self.names: dict[str, Any] = {}

    def __call__(
        self,
        app: ASGIApp,
        *declarations: RouteDeclaration,
        name: Any = None,  # noqa: ANN401
        door: bool = False,
    ) -> ASGIApp:
        """Record the call, and return `app` as it is."""
        self.calls.append((declarations, door))
        self.names[declarations[0].path] = name
        return app

    @property
    def paths(self) -> list[str]:
        """Return the path of each call, in order."""
        return [declarations[0].path for declarations, _ in self.calls]


class TestTheGateContract:
    """What `install_route_gate` hands the gate for each route FastAPI dispatches to."""

    def test_each_route_is_declared_under_each_include(self) -> None:
        """With the scopes of every include around it, websocket routes too."""
        inner = APIRouter(dependencies=[Authenticated(scopes=["inner"])])
        inner.add_api_route("/r", listed, methods=["GET"])
        inner.add_api_websocket_route(
            "/ws", accept, dependencies=[Authenticated(scopes=["live"])]
        )
        middle = APIRouter()
        middle.include_router(
            inner, prefix="/in", dependencies=[Authenticated(scopes=["mid"])]
        )
        app = FastAPI(openapi_url=None)
        app.include_router(
            middle, prefix="/a", dependencies=[Authenticated(scopes=["outer"])]
        )
        app.include_router(middle, prefix="/b")
        gate = Recording()

        install_route_gate(app, gate)

        assert gate.calls == [
            (
                (
                    RouteDeclaration(
                        "/a/in/r",
                        methods=frozenset({"GET"}),
                        scopes=frozenset({"inner", "mid", "outer"}),
                    ),
                ),
                False,
            ),
            (
                (
                    RouteDeclaration(
                        "/a/in/ws",
                        scopes=frozenset({"inner", "live", "mid", "outer"}),
                    ),
                ),
                False,
            ),
            (
                (
                    RouteDeclaration(
                        "/b/in/r",
                        methods=frozenset({"GET"}),
                        scopes=frozenset({"inner", "mid"}),
                    ),
                ),
                False,
            ),
            (
                (
                    RouteDeclaration(
                        "/b/in/ws",
                        scopes=frozenset({"inner", "live", "mid"}),
                    ),
                ),
                False,
            ),
        ]
        assert route_declarations(app) == [
            declarations[0] for declarations, _ in gate.calls
        ]

    @needs_frontend
    def test_a_frontend_is_declared_after_every_other_route(
        self, site: str
    ) -> None:
        """As one route answering every request."""
        app = FastAPI(openapi_url=None)
        app.frontend("/ui", directory=site)
        app.add_api_route("/r", listed, methods=["GET"])
        gate = Recording()

        install_route_gate(app, gate)

        assert gate.calls == [
            ((RouteDeclaration("/r", methods=frozenset({"GET"})),), False),
            ((RouteDeclaration("/"),), False),
        ]

    def test_a_mount_is_a_door_before_the_routes_it_holds(self) -> None:
        """Its routes are gated where they are, and it is listed while none is anonymous."""
        sub = FastAPI(openapi_url=None)
        sub.add_api_route("/private", listed)
        app = FastAPI(openapi_url=None)
        app.mount("/sub", sub)
        gate = Recording()

        install_route_gate(app, gate)

        assert gate.calls == [
            (
                (RouteDeclaration("/sub/private", methods=frozenset({"GET"})),),
                False,
            ),
            ((RouteDeclaration("/sub"),), True),
        ]
        assert route_declarations(app) == [
            RouteDeclaration("/sub"),
            RouteDeclaration("/sub/private", methods=frozenset({"GET"})),
        ]

    def test_a_refusal_is_named_by_the_route_under_the_root_path(self) -> None:
        """An include's route under its prefix, a mounted app's under the mount."""
        router = APIRouter()
        router.add_api_route("/orders/{order_id}", listed)
        sub = FastAPI(openapi_url=None)
        sub.add_api_route("/private", listed)
        app = FastAPI(openapi_url=None)
        app.include_router(router, prefix="/v1")
        app.mount("/sub", sub)
        gate = Recording()
        install_route_gate(app, gate)

        included = gate.names["/v1/orders/{order_id}"]
        mounted = gate.names["/sub/private"]

        assert included({"root_path": "/svc", "path": "/svc/v1/orders/7"}) == (
            "/svc/v1/orders/{order_id}"
        )
        assert mounted({"root_path": "/sub", "path": "/sub/private"}) == (
            "/sub/private"
        )

    def test_a_route_added_to_an_include_is_gated_with_the_contexts_built_anew(
        self,
    ) -> None:
        """Under each include, once FastAPI dispatches through it."""
        router = APIRouter()
        router.add_api_route("/r", listed)
        app = FastAPI(openapi_url=None)
        app.include_router(router, prefix="/a")
        app.include_router(router, prefix="/b", dependencies=[Anonymous()])
        gate = Recording()
        install_route_gate(app, gate)
        client = TestClient(app)
        client.get("/a/r")
        gate.calls.clear()

        router.add_api_route("/late", listed)
        client.get("/a/late")
        client.get("/b/late")

        assert gate.paths == ["/a/r", "/a/late", "/b/r", "/b/late"]
        assert [
            declarations[0].anonymous for declarations, _ in gate.calls
        ] == [
            False,
            False,
            True,
            True,
        ]


def checked(*dependencies: params.Depends) -> RouteDeclaration:
    """Return what a `GET /r` route with `dependencies` declares."""
    app = FastAPI(openapi_url=None)
    app.add_api_route("/r", listed, dependencies=list(dependencies))
    return route_declarations(app)[0]


def page(number: int = 1) -> int:
    """Read a query parameter, a dependency of the route's own."""
    return number  # pragma: no cover


class TestDeclarations:
    """What `route_declarations` reads off the dependencies each route runs."""

    def test_grelmicro_reading_the_caller_is_no_check_of_its_own(self) -> None:
        """`Authenticated`, `CurrentPrincipal`, `Claims`, `CurrentToken` and `OptionalPrincipal`."""
        app = FastAPI(openapi_url=None)

        @app.get("/me", dependencies=[Authenticated(scopes=["me:read"])])
        async def me(
            principal: CurrentPrincipal,
            claims: Claims,
            presented: CurrentToken,
            maybe: OptionalPrincipal,
            scoped: Annotated[Principal, Authenticated(scopes=["orders:read"])],
        ) -> None: ...

        assert route_declarations(app) == [
            RouteDeclaration(
                "/me",
                methods=frozenset({"GET"}),
                scopes=frozenset({"me:read", "orders:read"}),
            )
        ]

    def test_a_dependency_of_the_app_is_a_check_of_its_own(self) -> None:
        """Whatever it reads, the route's answer can depend on it, a `Security` included."""
        assert checked(Depends(page)).own_checks
        assert checked(Security(listed, scopes=[])).own_checks

    def test_a_route_caching_beside_an_authenticated_caller_caches(
        self,
    ) -> None:
        """Every caller the route admits is served the same response."""
        assert checked(Authenticated(), CachedResponse(ttl=60)) == (
            RouteDeclaration("/r", methods=frozenset({"GET"}), cache=60)
        )

    @pytest.mark.parametrize("authenticated", [False, True])
    def test_a_route_caching_beside_a_check_of_its_own_fails_install(
        self, *, authenticated: bool
    ) -> None:
        """One caller's response would be served to another, with authentication or without."""
        app = FastAPI(openapi_url=None)
        app.add_api_route(
            "/r", listed, dependencies=[Depends(page), CachedResponse()]
        )
        uses = [
            ErrorResponses(),
            Cache(MemoryCacheAdapter()),
            CachedResponses(),
        ]
        if authenticated:
            uses.append(AuthenticatedRequests(verifier()))

        with pytest.raises(
            TypeError,
            match=r"CachedResponse\(\) is declared on '/r', which runs",
        ):
            Grelmicro(uses=uses).install(app)

    @pytest.mark.parametrize("authenticated", [False, True])
    def test_a_router_caching_beside_a_check_of_its_own_installs(
        self, *, authenticated: bool
    ) -> None:
        """The read running the check is left to its handler."""
        router = APIRouter(dependencies=[CachedResponse()])
        router.add_api_route("/r", listed, dependencies=[Depends(page)])
        app = FastAPI(openapi_url=None)
        app.include_router(router)
        uses = [
            ErrorResponses(),
            Cache(MemoryCacheAdapter()),
            CachedResponses(),
        ]
        if authenticated:
            uses.append(AuthenticatedRequests(verifier()))

        Grelmicro(uses=uses).install(app)

        assert TestClient(app).get("/r", headers=bearer(token())).json() == {
            "listed": True
        }

    @pytest.mark.parametrize("where", ["router", "include", "app"])
    def test_a_router_caching_covers_the_reads_that_run_no_check(
        self, where: str
    ) -> None:
        """Its writes and its reads running a check of their own declare no cache."""
        cached = CachedResponse(ttl=30)
        router = APIRouter(dependencies=[cached] if where == "router" else [])
        router.add_api_route("/read", listed)
        router.add_api_route("/paged", listed, dependencies=[Depends(page)])
        router.add_api_route("/write", listed, methods=["POST"])
        app = FastAPI(
            openapi_url=None, dependencies=[cached] if where == "app" else []
        )
        app.include_router(
            router, dependencies=[cached] if where == "include" else []
        )

        assert [
            (declaration.path, declaration.cache)
            for declaration in route_declarations(installed(app))
        ] == [("/read", 30), ("/paged", False), ("/write", False)]
