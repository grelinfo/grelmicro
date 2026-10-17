"""The response cache, idempotency and conditional requests run at the route, on every app.

The cache and idempotency run on FastAPI, Starlette and Litestar, on an
app with `AuthenticatedRequests` and on one without, with the component
registered alone and beside the other one. A route is declared directly,
on a mounted app, through a router, and after `micro.install(app)`. A
Litestar ASGI mount runs checks of its own, so it is never cached or
replayed. A URL no route answers is answered `200` by the app's not-found
handler, so the only thing that keeps it out of the cache and the
idempotency store is that no route's gate ran.

On an app without authentication, the open gate also:

- reads no route tree per request, and fails startup on a listed route
  that carries no gate,
- keeps a route an authenticated app shares refused, whichever app is
  installed first,
- leaves the rate limit in front of the router, so unknown URLs count,
- raises what a middleware raises at the route where it handed the
  request on, and runs on every app one component is installed on,
- leaves a component the app runs behind Litestar's router to answer
  there.

Conditional requests answer `404` for an unknown URL and `405` for a
method the route does not take, `428` and `412` on the route, and `304`
on a cache hit whichever of the two is registered first. An integration
that gates no route leaves the cache in front of the router.

On every app, with authentication or without, an `include=` pattern never
caches a route under checks above it: middleware on a mount, a mounted app
with middleware of its own, or a Litestar router with middleware. A second
tenant never gets the first tenant's body.

FastAPI declares the cache with `CachedResponse()`. Starlette and Litestar
name the paths in `include=`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from fastapi import APIRouter, FastAPI
from litestar import Litestar, asgi, get, post, put
from litestar import Request as LitestarRequest
from litestar import Response as LitestarResponse
from litestar import Router as LitestarRouter
from litestar.exceptions import NotFoundException
from litestar.middleware import DefineMiddleware
from litestar.routes import HTTPRoute
from litestar.testing import TestClient as LitestarClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient as StarletteClient

from grelmicro import Grelmicro
from grelmicro._paths import _RouteTopologyState
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    AuthenticatedRequests,
    CachedResponses,
    ConditionalRequests,
    ErrorResponses,
    IdempotentRequests,
    RateLimitedRequests,
    check_precondition,
)
from grelmicro.idempotency.errors import IdempotencyKeyFunctionError
from grelmicro.integrations import fastapi as on_fastapi
from grelmicro.integrations import litestar as on_litestar
from grelmicro.integrations import starlette as on_starlette
from grelmicro.providers.memory import MemoryProvider
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.security import TrustedProxies
from tests.test_authentication import bearer, token, verifier

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

pytestmark = [pytest.mark.timeout(10)]

OK = 200
NOT_MODIFIED = 304
UNAUTHORIZED = 401
NOT_FOUND = 404
METHOD_NOT_ALLOWED = 405
PRECONDITION_FAILED = 412
PRECONDITION_REQUIRED = 428
TOO_MANY_REQUESTS = 429
VERSION = 1
"""The version `PUT /feed` holds, which `check_precondition` compares."""
HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"

Framework = Literal["fastapi", "starlette", "litestar"]
FRAMEWORKS: tuple[Framework, ...] = ("fastapi", "starlette", "litestar")
Component = Literal["cache", "idempotency"]

PATHS = {
    "declared route": "",
    "mounted app": "/sub",
    "router declaration": "/v1",
    "route added after install": "/later",
}
"""Where each kind of route sits: its read is `{prefix}/feed`, its write `{prefix}/charge`."""

EXCLUDED = ("/feed", "/charge", "/sub/*", "/v1/*", "/later/*")
"""The paths Starlette serves without a credential, having no `Anonymous()`."""

CACHED = {
    "/feed": 60,
    "/sub/*": 60,
    "/v1/*": 60,
    "/later/*": 60,
    "/missing": 60,
}
"""What Starlette and Litestar name in `include=`: every read, and the unknown URL."""


class Calls:
    """Counts what each handler of an app served."""

    def __init__(self) -> None:
        """Start every count at zero."""
        self.counts: dict[str, int] = {}

    def served(self, name: str) -> dict[str, int]:
        """Count one call of `name`, and return what its handler answers."""
        self.counts[name] = self.counts.get(name, 0) + 1
        return {"calls": self.counts[name]}


def _fastapi_pair(
    target: Any,  # noqa: ANN401
    prefix: str,
    calls: Calls,
    *,
    read: list[Any] | None,
    write: list[Any] | None,
    at_root: bool = False,
) -> None:
    """Add `{prefix}/feed` and `{prefix}/charge` to a FastAPI app or router.

    Either is left out when its dependencies are `None`. A router, and an
    app `at_root`, adds them without the prefix, which its mount adds.
    """

    async def feed() -> dict[str, int]:
        return calls.served(f"{prefix}/feed" if prefix else "feed")

    async def charge() -> dict[str, int]:
        return calls.served(f"{prefix}/charge" if prefix else "charge")

    under = "" if isinstance(target, APIRouter) or at_root else prefix
    if read is not None:
        target.add_api_route(f"{under}/feed", feed, dependencies=read)
    if write is not None:
        target.add_api_route(
            f"{under}/charge", charge, methods=["POST"], dependencies=write
        )


def _fastapi(
    calls: Calls, *, authenticated: bool
) -> tuple[Any, Callable[[], None]]:
    """Return a FastAPI app, and what adds its route after install."""
    marks = [on_fastapi.Anonymous()] if authenticated else []
    cached = on_fastapi.CachedResponse(ttl=60)

    async def missing(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(calls.served("missing"))

    app = FastAPI(openapi_url=None, exception_handlers={404: missing})
    _fastapi_pair(app, "", calls, read=[*marks, cached], write=marks)
    sub = FastAPI(openapi_url=None)
    _fastapi_pair(
        sub, "/sub", calls, read=[*marks, cached], write=marks, at_root=True
    )
    app.mount("/sub", sub)
    reads = APIRouter(prefix="/v1", dependencies=[cached])
    writes = APIRouter(prefix="/v1")
    _fastapi_pair(reads, "/v1", calls, read=marks, write=None)
    app.include_router(reads)
    _fastapi_pair(writes, "/v1", calls, read=None, write=marks)
    app.include_router(writes)

    def later() -> None:
        _fastapi_pair(app, "/later", calls, read=[*marks, cached], write=marks)

    return app, later


def _starlette(
    calls: Calls,
    *,
    authenticated: bool,  # noqa: ARG001
) -> tuple[Any, Callable[[], None]]:
    """Return a Starlette app, and what adds its route after install."""

    def answering(name: str) -> Callable[[Request], Any]:
        async def endpoint(request: Request) -> JSONResponse:  # noqa: ARG001
            return JSONResponse(calls.served(name))

        return endpoint

    async def missing(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(calls.served("missing"))

    def pair(prefix: str) -> list[Route]:
        return [
            Route("/feed", answering(f"{prefix}/feed" if prefix else "feed")),
            Route(
                "/charge",
                answering(f"{prefix}/charge" if prefix else "charge"),
                methods=["POST"],
            ),
        ]

    app = Starlette(
        routes=[
            *pair(""),
            Mount("/sub", app=Starlette(routes=pair("/sub"))),
            Mount("/v1", app=Router(pair("/v1"))),
        ],
        exception_handlers={404: missing},
    )

    def later() -> None:
        app.add_route("/later/feed", answering("/later/feed"))
        app.add_route(
            "/later/charge", answering("/later/charge"), methods=["POST"]
        )

    return app, later


def _litestar(
    calls: Calls, *, authenticated: bool
) -> tuple[Any, Callable[[], None]]:
    """Return a Litestar app, and what adds its route after install."""
    opt: dict[str, Any] = (
        {"opt": on_litestar.Anonymous()} if authenticated else {}
    )

    def pair(prefix: str) -> list[Any]:
        @get("/feed", name=f"{prefix}feed", sync_to_thread=False, **opt)
        def feed() -> dict[str, int]:
            return calls.served(f"{prefix}/feed" if prefix else "feed")

        @post(
            "/charge",
            name=f"{prefix}charge",
            status_code=OK,
            sync_to_thread=False,
            **opt,
        )
        def charge() -> dict[str, int]:
            return calls.served(f"{prefix}/charge" if prefix else "charge")

        return [feed, charge]

    @asgi("/sub", is_mount=True, copy_scope=True, **opt)
    async def sub(scope: Scope, receive: Receive, send: Send) -> None:
        name = "/sub/feed" if scope["method"] == "GET" else "/sub/charge"
        await JSONResponse(calls.served(name))(scope, receive, send)

    def missing(request: Any, exc: Exception) -> LitestarResponse[Any]:  # noqa: ANN401, ARG001
        return LitestarResponse(calls.served("missing"), status_code=OK)

    app = Litestar(
        [*pair(""), sub, LitestarRouter("/v1", route_handlers=pair("/v1"))],
        exception_handlers={NotFoundException: missing},
    )

    def later() -> None:
        app.register(LitestarRouter("/later", route_handlers=pair("/later")))

    return app, later


def built(
    framework: Framework,
    *,
    authenticated: bool,
    components: tuple[Component, ...],
    calls: Calls,
    ahead: tuple[Any, ...] = (),
) -> tuple[Any, Callable[[], None]]:
    """Return an app with `components` installed, and what adds its route after install.

    `ahead` is registered before them.
    """
    build: Callable[..., tuple[Any, Callable[[], None]]] = {
        "fastapi": _fastapi,
        "starlette": _starlette,
        "litestar": _litestar,
    }[framework]
    app, later = build(calls, authenticated=authenticated)
    uses: list[Any] = [ErrorResponses(), MemoryProvider(), *ahead]
    if authenticated:
        uses.append(
            AuthenticatedRequests(
                verifier(),
                exclude=EXCLUDED if framework == "starlette" else (),
            )
        )
    if "cache" in components:
        uses += [
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/missing": 60})
            if framework == "fastapi"
            else CachedResponses(include=CACHED),
        ]
    if "idempotency" in components:
        uses.append(IdempotentRequests())
    Grelmicro(uses=uses).install(app)
    return app, later


def client_of(app: Any) -> Any:  # noqa: ANN401
    """Return a test client of `app`."""
    if isinstance(app, Litestar):
        return LitestarClient(app)
    return StarletteClient(app)


def components_of(
    component: Component, registered: str
) -> tuple[Component, ...]:
    """Return `component`, and the other one when it is registered beside it."""
    other: Component = "idempotency" if component == "cache" else "cache"
    if registered == "beside the other":
        return (component, other)
    return (component,)


def checks_its_own(framework: Framework, where: str) -> bool:
    """Return whether the route runs checks of its own, so nothing is shared.

    A Litestar ASGI mount serves an app the integration cannot read, so it
    is declared as running checks of its own.
    """
    return framework == "litestar" and where == "mounted app"


matrix = pytest.mark.parametrize(
    ("framework", "authentication", "registered"),
    [
        (framework, authentication, registered)
        for framework in FRAMEWORKS
        for authentication in ("without authentication", "with authentication")
        for registered in ("alone", "beside the other")
    ],
)
routes = pytest.mark.parametrize("where", list(PATHS))


@matrix
@routes
def test_cached_responses_route_is_served_from_cache_on_second_request(
    framework: Framework, authentication: str, registered: str, where: str
) -> None:
    """The route's gate ran, so its declaration or the pattern naming it caches."""
    # Arrange
    calls = Calls()
    app, later = built(
        framework,
        authenticated=authentication == "with authentication",
        components=components_of("cache", registered),
        calls=calls,
    )
    if where == "route added after install":
        later()
    path = f"{PATHS[where]}/feed"

    # Act
    with client_of(app) as client:
        first = client.get(path)
        second = client.get(path)

    # Assert
    assert first.status_code == second.status_code == OK
    if checks_its_own(framework, where):
        assert second.json() == {"calls": 2}
        assert "age" not in second.headers
    else:
        assert first.json() == second.json() == {"calls": 1}
        assert "age" in second.headers


@matrix
def test_cached_responses_unknown_url_is_never_cached(
    framework: Framework, authentication: str, registered: str
) -> None:
    """No route's gate ran, so a pattern naming the URL caches nothing."""
    # Arrange
    calls = Calls()
    app, _ = built(
        framework,
        authenticated=authentication == "with authentication",
        components=components_of("cache", registered),
        calls=calls,
    )

    # Act
    with client_of(app) as client:
        first = client.get("/missing")
        second = client.get("/missing")

    # Assert
    assert "age" not in second.headers
    if authentication == "with authentication":
        assert first.status_code == second.status_code == UNAUTHORIZED
    else:
        assert first.json() == {"calls": 1}
        assert second.json() == {"calls": 2}


@matrix
@routes
def test_idempotent_requests_route_replays_on_second_request(
    framework: Framework, authentication: str, registered: str, where: str
) -> None:
    """The route's gate ran, so a repeated key replays what it stored."""
    # Arrange
    calls = Calls()
    app, later = built(
        framework,
        authenticated=authentication == "with authentication",
        components=components_of("idempotency", registered),
        calls=calls,
    )
    if where == "route added after install":
        later()
    path = f"{PATHS[where]}/charge"

    # Act
    with client_of(app) as client:
        first = client.post(path, headers={HEADER: "k"})
        second = client.post(path, headers={HEADER: "k"})

    # Assert
    assert first.status_code == second.status_code == OK
    if checks_its_own(framework, where):
        assert second.json() == {"calls": 2}
        assert REPLAY_HEADER not in second.headers
    else:
        assert first.json() == second.json() == {"calls": 1}
        assert second.headers[REPLAY_HEADER] == "true"


@matrix
def test_idempotent_requests_unknown_url_is_never_replayed(
    framework: Framework, authentication: str, registered: str
) -> None:
    """No route's gate ran, so the key stores and replays nothing."""
    # Arrange
    calls = Calls()
    app, _ = built(
        framework,
        authenticated=authentication == "with authentication",
        components=components_of("idempotency", registered),
        calls=calls,
    )

    # Act
    with client_of(app) as client:
        first = client.post("/missing", headers={HEADER: "k"})
        second = client.post("/missing", headers={HEADER: "k"})

    # Assert
    assert REPLAY_HEADER not in second.headers
    if authentication == "with authentication":
        assert first.status_code == second.status_code == UNAUTHORIZED
    else:
        assert first.json() == {"calls": 1}
        assert second.json() == {"calls": 2}


@pytest.mark.parametrize("framework", FRAMEWORKS)
@pytest.mark.parametrize("component", ["cache", "idempotency"])
def test_open_gate_without_authentication_reads_no_route_tree_per_request(
    framework: Framework,
    component: Component,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declaration rides the request, so the routes are never re-read for it."""
    # Arrange
    calls = Calls()
    app, _ = built(
        framework,
        authenticated=False,
        components=(component,),
        calls=calls,
    )
    reads: list[object] = []
    monkeypatch.setattr(_RouteTopologyState, "changed", reads.append)

    # Act
    with client_of(app) as client:
        if component == "cache":
            client.get("/feed")
            client.get("/feed")
        else:
            client.post("/charge", headers={HEADER: "k"})
            client.post("/charge", headers={HEADER: "k"})

    # Assert
    assert reads == []


def _authenticated_app(routes: list[Any]) -> Starlette:
    """Return a Starlette app serving `routes` behind `AuthenticatedRequests`."""
    app = Starlette(routes=routes)
    Grelmicro(
        uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
    ).install(app)
    return app


def _cached_app(routes: list[Any]) -> Starlette:
    """Return a Starlette app serving `routes` with the cache and no authentication."""
    app = Starlette(routes=routes)
    Grelmicro(
        uses=[
            ErrorResponses(),
            MemoryProvider(),
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=("/r/*",)),
            IdempotentRequests(),
        ]
    ).install(app)
    return app


@pytest.mark.parametrize("first", ["authenticated", "without authentication"])
def test_cached_responses_route_shared_with_an_authenticated_app_fails_closed(
    first: str,
) -> None:
    """Whichever app is installed first, the shared route keeps its authentication gate."""
    # Arrange
    calls = Calls()

    async def feed(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(calls.served("feed"))

    shared = Router(
        [Route("/feed", feed), Route("/feed", feed, methods=["POST"])]
    )
    if first == "authenticated":
        authenticated = _authenticated_app([Mount("/r", app=shared)])
        bare = _cached_app([Mount("/r", app=shared)])
    else:
        bare = _cached_app([Mount("/r", app=shared)])
        authenticated = _authenticated_app([Mount("/r", app=shared)])

    # Act
    with StarletteClient(bare) as client:
        reads = [client.get("/r/feed") for _ in range(2)]
        writes = [
            client.post("/r/feed", headers={HEADER: "k"}) for _ in range(2)
        ]
    with StarletteClient(authenticated) as client:
        caller = client.get("/r/feed", headers=bearer(token()))

    # Assert
    assert [reply.status_code for reply in reads + writes] == [UNAUTHORIZED] * 4
    assert all(REPLAY_HEADER not in reply.headers for reply in writes)
    assert caller.json() == {"calls": 1}


def test_rate_limited_requests_without_authentication_counts_unknown_urls() -> (
    None
):
    """The rate limit stays in front of the router, so a flood of unknown URLs is counted."""
    # Arrange
    limit = RateLimitedRequests(
        RateLimiter.sliding_window(
            "burst", limit=2, window=60, backend=MemoryRateLimiterAdapter()
        ),
        trusted=TrustedProxies(["10.0.0.0/8"]),
    )
    app, _ = built(
        "starlette",
        authenticated=False,
        components=("cache", "idempotency"),
        calls=Calls(),
        ahead=(limit,),
    )

    # Act
    with StarletteClient(app, client=("203.0.113.7", 5000)) as client:
        statuses = [client.get("/nowhere").status_code for _ in range(3)]

    # Assert
    assert statuses[-1] == TOO_MANY_REQUESTS


@pytest.mark.parametrize("component", ["cache", "idempotency"])
def test_open_gate_route_carrying_no_gate_fails_startup(
    component: Component,
) -> None:
    """A route that reached the route table past the integration would never run them."""
    # Arrange
    calls = Calls()
    app, _ = built(
        "starlette", authenticated=False, components=(component,), calls=calls
    )

    async def late(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({})  # pragma: no cover

    list.append(app.router.routes, Route("/late", late))

    # Act / Assert
    with (
        pytest.raises(RuntimeError, match="GET HEAD /late carries no gate"),
        StarletteClient(app),
    ):
        pass  # pragma: no cover


def test_open_gate_litestar_handler_carrying_no_gate_fails_startup() -> None:
    """A Litestar handler the router never built its trie for is found too."""
    # Arrange
    calls = Calls()
    app, _ = built(
        "litestar", authenticated=False, components=("cache",), calls=calls
    )

    @get("/late", sync_to_thread=False)
    def late() -> None: ...  # pragma: no cover

    app.routes.append(HTTPRoute(path="/late", route_handlers=[late]))

    # Act / Assert
    with (
        pytest.RaisesGroup(
            pytest.RaisesExc(RuntimeError, match="GET /late carries no gate")
        ),
        LitestarClient(app),
    ):
        pass  # pragma: no cover


def test_idempotent_requests_error_at_the_route_is_raised_where_it_was_handed_on() -> (
    None
):
    """What it raises at the route passes the cache's edge and surfaces as raised."""
    # Arrange
    calls = Calls()
    app, _ = built(
        "starlette",
        authenticated=False,
        components=(),
        calls=calls,
        ahead=(
            IdempotentRequests(methods=["GET"], key=lambda scope, key: ""),  # noqa: ARG005
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=CACHED),
        ),
    )

    # Act / Assert
    with (
        StarletteClient(app) as client,
        pytest.raises(IdempotencyKeyFunctionError, match="empty string"),
    ):
        client.get("/feed", headers={HEADER: "k"})


def test_cached_responses_runs_at_the_routes_of_each_app_it_is_installed_on() -> (
    None
):
    """One component installed on two apps without authentication caches on both."""
    # Arrange
    calls = Calls()

    def answering(name: str) -> Callable[[Request], Any]:
        async def endpoint(request: Request) -> JSONResponse:  # noqa: ARG001
            return JSONResponse(calls.served(name))

        return endpoint

    first = Starlette(routes=[Route("/feed", answering("first"))])
    second = Starlette(routes=[Route("/feed", answering("second"))])
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=("/feed",)),
        ]
    )
    micro.install(first)
    micro.install(second)

    # Act
    replies = []
    for app in (first, second):
        with StarletteClient(app) as client:
            replies += [client.get("/feed"), client.get("/feed")]

    # Assert
    assert [reply.json() for reply in replies] == [{"calls": 1}] * 4


def test_cached_responses_integration_gating_no_route_answers_before_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An integration that lists its routes and gates none leaves the cache in front."""
    # Arrange
    calls = Calls()

    async def missing(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(calls.served("missing"))

    app = Starlette(exception_handlers={404: missing})

    def install_middleware(app: Any, components: Any) -> None:  # noqa: ANN401
        for component in components:
            middleware, options = component.asgi_middleware()
            app.user_middleware.append(Middleware(middleware, **options))

    integration = SimpleNamespace(
        install=on_starlette.install,
        is_bound=on_starlette.is_bound,
        install_middleware=install_middleware,
        route_declarations=on_starlette.route_declarations,
    )
    monkeypatch.setattr(
        "grelmicro._app.load_integration",
        lambda app: integration,  # noqa: ARG005
    )
    Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), CachedResponses(include=CACHED)]
    ).install(app)

    # Act
    with StarletteClient(app) as client:
        first = client.get("/missing")
        second = client.get("/missing")

    # Assert
    assert first.json() == second.json() == {"calls": 1}
    assert "age" in second.headers


def _conditional_app(framework: Framework, calls: Calls) -> Any:  # noqa: ANN401
    """Return an app serving `GET /feed` and `PUT /feed`, uninstalled."""
    if framework == "litestar":

        @get("/feed", sync_to_thread=False)
        def read() -> dict[str, int]:
            return calls.served("feed")

        @put("/feed", sync_to_thread=False)
        def write() -> dict[str, int]:
            check_precondition(VERSION)
            return calls.served("write")

        return Litestar([read, write])
    if framework == "fastapi":
        app = FastAPI(openapi_url=None)

        @app.get("/feed")
        async def fastapi_read() -> dict[str, int]:
            return calls.served("feed")

        @app.put("/feed")
        async def fastapi_write() -> dict[str, int]:
            check_precondition(VERSION)
            return calls.served("write")

        return app

    async def starlette_read(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(calls.served("feed"))

    async def starlette_write(request: Request) -> JSONResponse:  # noqa: ARG001
        check_precondition(VERSION)
        return JSONResponse(calls.served("write"))

    return Starlette(
        routes=[
            Route("/feed", starlette_read, methods=["GET"]),
            Route("/feed", starlette_write, methods=["PUT"]),
        ]
    )


def conditional(
    framework: Framework, *, before_cache: bool = True
) -> tuple[Any, Calls]:
    """Return an app with `ConditionalRequests` and the cache, and its calls.

    `PUT` and `DELETE` require a precondition. `before_cache` registers the
    conditional requests before the cache, and after it otherwise.
    """
    calls = Calls()
    app = _conditional_app(framework, calls)
    preconditions = ConditionalRequests(require_precondition=("PUT", "DELETE"))
    cached = CachedResponses(include=("/feed",))
    ordered = (
        [preconditions, cached] if before_cache else [cached, preconditions]
    )
    Grelmicro(
        uses=[ErrorResponses(), Cache(MemoryCacheAdapter()), *ordered]
    ).install(app)
    return app, calls


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_conditional_requests_unknown_url_without_authentication_is_404(
    framework: Framework,
) -> None:
    """No route's gate ran, so a write it requires a precondition of is not refused 428."""
    # Arrange
    app, _ = conditional(framework)

    # Act
    with client_of(app) as client:
        reply = client.put("/missing")

    # Assert
    assert reply.status_code == NOT_FOUND


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_conditional_requests_method_the_route_does_not_take_is_405(
    framework: Framework,
) -> None:
    """The router answers a method no route takes before any precondition is asked."""
    # Arrange
    app, _ = conditional(framework)

    # Act
    with client_of(app) as client:
        reply = client.delete("/feed")

    # Assert
    assert reply.status_code == METHOD_NOT_ALLOWED


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_conditional_requests_declared_route_without_a_precondition_is_428(
    framework: Framework,
) -> None:
    """A route that takes the write still refuses it without `If-Match`."""
    # Arrange
    app, calls = conditional(framework)

    # Act
    with client_of(app) as client:
        reply = client.put("/feed")

    # Assert
    assert reply.status_code == PRECONDITION_REQUIRED
    assert "write" not in calls.counts


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_conditional_requests_stale_if_match_is_412(
    framework: Framework,
) -> None:
    """The handler's own check still answers a stale tag at the route."""
    # Arrange
    app, calls = conditional(framework)

    # Act
    with client_of(app) as client:
        reply = client.put("/feed", headers={"If-Match": '"stale"'})

    # Assert
    assert reply.status_code == PRECONDITION_FAILED
    assert "write" not in calls.counts


@pytest.mark.parametrize("framework", FRAMEWORKS)
@pytest.mark.parametrize(
    "order", ["conditional before cache", "conditional after cache"]
)
def test_conditional_requests_cache_hit_answers_304_to_a_held_tag(
    framework: Framework, order: str
) -> None:
    """In either order, a hit is served once and a held tag gets `304`."""
    # Arrange
    app, calls = conditional(
        framework, before_cache=order == "conditional before cache"
    )

    # Act
    with client_of(app) as client:
        first = client.get("/feed")
        hit = client.get("/feed")
        held = client.get(
            "/feed", headers={"If-None-Match": first.headers["etag"]}
        )

    # Assert
    assert first.json() == hit.json() == {"calls": 1}
    assert hit.headers["etag"] == first.headers["etag"]
    assert held.status_code == NOT_MODIFIED
    assert calls.counts == {"feed": 1}


@pytest.mark.parametrize("component", ["cache", "idempotency", "conditional"])
def test_open_gate_litestar_middleware_behind_the_router_answers_there(
    component: str,
) -> None:
    """A component's middleware the app runs behind its router answers there, once."""
    # Arrange
    calls = Calls()

    @get("/feed", sync_to_thread=False)
    def read() -> dict[str, int]:
        return calls.served("feed")

    @post("/charge", status_code=OK, sync_to_thread=False)
    def charge() -> dict[str, int]:
        return calls.served("charge")

    @put("/feed", sync_to_thread=False)
    def write() -> dict[str, int]:
        return calls.served("write")  # pragma: no cover

    registered: Any = {
        "cache": CachedResponses(include=("/feed",)),
        "idempotency": IdempotentRequests(),
        "conditional": ConditionalRequests(require_precondition=("PUT",)),
    }[component]
    middleware, options = registered.asgi_middleware()
    app = Litestar(
        [read, charge, write],
        middleware=[DefineMiddleware(middleware, **options)],
    )
    Grelmicro(
        uses=[
            ErrorResponses(),
            MemoryProvider(),
            Cache(MemoryCacheAdapter()),
            registered,
        ]
    ).install(app)

    # Act
    with LitestarClient(app) as client:
        if component == "cache":
            replies = [client.get("/feed"), client.get("/feed")]
        elif component == "idempotency":
            replies = [
                client.post("/charge", headers={HEADER: "k"}) for _ in range(2)
            ]
        else:
            replies = [client.put("/feed"), client.put("/missing")]

    # Assert
    if component == "cache":
        assert [reply.json() for reply in replies] == [{"calls": 1}] * 2
    elif component == "idempotency":
        assert [reply.json() for reply in replies] == [{"calls": 1}] * 2
        assert replies[1].headers[REPLAY_HEADER] == "true"
    else:
        assert [reply.status_code for reply in replies] == [
            PRECONDITION_REQUIRED,
            NOT_FOUND,
        ]


class TenantCheck:
    """A middleware that admits a request naming its tenant, as a check above the route."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        """Wrap `app`."""
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Pass the request on with its tenant, or refuse one naming none."""
        if scope["type"] == "http" and b"x-tenant" not in dict(
            scope["headers"]
        ):
            await JSONResponse({}, status_code=UNAUTHORIZED)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


async def _tenant(request: Request) -> JSONResponse:
    """Answer the tenant the request names."""
    return JSONResponse({"tenant": request.headers["x-tenant"]})


def _checked_mount(parent: str) -> Any:  # noqa: ANN401
    """Return an app mounting a router at `/m` behind middleware on the mount."""
    mount = Mount(
        "/m",
        app=Router([Route("/feed", _tenant)]),
        middleware=[Middleware(TenantCheck)],
    )
    if parent == "fastapi":
        return FastAPI(openapi_url=None, routes=[mount])
    return Starlette(routes=[mount])


def _checked_sub_app(parent: str) -> Any:  # noqa: ANN401
    """Return an app mounting a Starlette app with middleware of its own at `/m`."""
    sub = Starlette(
        routes=[Route("/feed", _tenant)], middleware=[Middleware(TenantCheck)]
    )
    mount = Mount("/m", app=sub)
    if parent == "fastapi":
        return FastAPI(openapi_url=None, routes=[mount])
    return Starlette(routes=[mount])


def _checked_litestar_router(parent: str) -> Any:  # noqa: ANN401, ARG001
    """Return a Litestar app whose router at `/m` runs middleware of its own."""

    @get("/feed", opt=on_litestar.Anonymous())
    async def feed(request: LitestarRequest) -> dict[str, str]:
        return {"tenant": request.headers["x-tenant"]}

    return Litestar(
        [
            LitestarRouter(
                "/m",
                route_handlers=[feed],
                middleware=[DefineMiddleware(cast("Any", TenantCheck))],
            )
        ]
    )


LEAK_SHAPES = {
    "middleware on the mount": _checked_mount,
    "mounted app with middleware of its own": _checked_sub_app,
    "litestar router with middleware": _checked_litestar_router,
}
"""Where checks run above a route that a pattern names."""


@pytest.mark.parametrize("shape", list(LEAK_SHAPES))
@pytest.mark.parametrize("parent", ["starlette", "fastapi", "litestar"])
@pytest.mark.parametrize(
    "authentication", ["without authentication", "with authentication"]
)
def test_cached_responses_route_checked_above_never_serves_another_tenant(
    shape: str, parent: str, authentication: str
) -> None:
    """A pattern naming a route under checks of their own never shares its body."""
    # Arrange
    if (parent == "litestar") != (shape == "litestar router with middleware"):
        pytest.skip("the shape is built on its own framework")
    app = LEAK_SHAPES[shape](parent)
    uses: list[Any] = [ErrorResponses(), Cache(MemoryCacheAdapter())]
    if authentication == "with authentication":
        uses.append(
            AuthenticatedRequests(
                verifier(), exclude=() if parent == "litestar" else ("/m/*",)
            )
        )
    uses.append(CachedResponses(include={"/m/*": 60}))
    Grelmicro(uses=uses).install(app)

    # Act
    with client_of(app) as client:
        first = client.get("/m/feed", headers={"x-tenant": "a"})
        second = client.get("/m/feed", headers={"x-tenant": "b"})

    # Assert
    assert first.json() == {"tenant": "a"}
    assert second.json() == {"tenant": "b"}
