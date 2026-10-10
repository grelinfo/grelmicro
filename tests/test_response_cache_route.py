"""The response cache at the route: a hit only reaches a caller the route admits.

`CachedResponses` runs in the lane of the route the framework matched,
once the route's gate admitted the request. It reads what the route
declares there, and keys what it stores by the protection the route
declares, so an entry stored while a route was anonymous is never served
once it is protected.

Each case runs on FastAPI, Starlette and Litestar. FastAPI declares the
cache on the route with `CachedResponse()`. Starlette and Litestar name the
path in `include=`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

import pytest
from fastapi import APIRouter, FastAPI
from litestar import Litestar, get
from litestar import Request as LitestarRequest
from litestar.di import NamedDependency, Provide
from litestar.testing import TestClient as LitestarClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient as StarletteClient

from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    AuthenticatedRequests,
    CachedResponses,
    CachedResponsesMiddleware,
    ErrorResponses,
)
from grelmicro.http._gate import DECLARATION_KEY
from grelmicro.integrations import fastapi as on_fastapi
from grelmicro.integrations import litestar as on_litestar
from grelmicro.integrations import starlette as on_starlette
from grelmicro.security.principal import Principal
from tests.test_authentication import bearer, token, verifier

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

pytestmark = [pytest.mark.timeout(10)]

OK = 200
UNAUTHORIZED = 401
FORBIDDEN = 403
SCOPE = "feed:read"

Framework = Literal["fastapi", "starlette", "litestar"]
Protection = Literal["anonymous", "protected", "scoped"]
FRAMEWORKS: tuple[Framework, ...] = ("fastapi", "starlette", "litestar")


class SharedStore(MemoryCacheAdapter):
    """A store that outlives the app, as a shared one does across a restart."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Keep every entry when the app stops."""


class Feed:
    """Counts what the `/feed` handler served, and names the app it ran in."""

    def __init__(self, name: str) -> None:
        """Start at zero, answering as `name`."""
        self.name = name
        self.calls = 0

    def serve(self) -> dict[str, Any]:
        """Count one call, and return what the handler answers."""
        self.calls += 1
        return {"app": self.name, "calls": self.calls}


def _fastapi(feed: Feed, protection: Protection) -> FastAPI:
    """Return a FastAPI app whose `/feed` declares the cache on the route."""
    guards = {
        "anonymous": [on_fastapi.Anonymous()],
        "protected": [],
        "scoped": [on_fastapi.Authenticated(scopes=[SCOPE])],
    }[protection]
    app = FastAPI(openapi_url=None)

    cached = on_fastapi.CachedResponse(ttl=60, shared=protection != "anonymous")

    @app.get("/feed", dependencies=[*guards, cached])
    async def read() -> dict[str, Any]:
        return feed.serve()

    return app


def _starlette(feed: Feed, protection: Protection) -> Starlette:
    """Return a Starlette app whose `/feed` the cache names in `include=`."""

    async def read(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(feed.serve())

    endpoint = (
        on_starlette.Authenticated(scopes=[SCOPE])(read)
        if protection == "scoped"
        else read
    )
    return Starlette(routes=[Route("/feed", endpoint)])


def _litestar(feed: Feed, protection: Protection) -> Litestar:
    """Return a Litestar app whose `/feed` the cache names in `include=`."""
    options: dict[str, Any] = {
        "anonymous": {"opt": on_litestar.Anonymous()},
        "protected": {},
        "scoped": {"guards": [on_litestar.Authenticated(scopes=[SCOPE])]},
    }[protection]

    @get("/feed", **options)
    async def read() -> dict[str, Any]:
        return feed.serve()

    return Litestar([read])


def served(
    framework: Framework,
    protection: Protection,
    store: MemoryCacheAdapter,
    feed: Feed,
) -> Any:  # noqa: ANN401
    """Return a client of an installed app serving `/feed`, caching into `store`.

    Starlette declares no anonymous route, so its anonymous `/feed` is a
    path the authentication excludes.
    """
    build: Callable[[Feed, Protection], Any] = {
        "fastapi": _fastapi,
        "starlette": _starlette,
        "litestar": _litestar,
    }[framework]
    app: Any = build(feed, protection)
    excluded = (
        ("/feed",)
        if framework == "starlette" and protection == "anonymous"
        else ()
    )
    cached = (
        CachedResponses()
        if framework == "fastapi"
        else CachedResponses(include={"/feed": 60})
    )
    Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(verifier(), exclude=excluded),
            Cache(store),
            cached,
        ]
    ).install(app)
    if framework == "litestar":
        return LitestarClient(app)
    return StarletteClient(app)


def stored_while_anonymous(
    framework: Framework, store: MemoryCacheAdapter
) -> None:
    """Store the anonymous `/feed` response in `store`, as the app before a restart."""
    feed = Feed("anonymous")
    with served(framework, "anonymous", store, feed) as client:
        first = client.get("/feed")
        second = client.get("/feed")
    assert first.json() == second.json() == {"app": "anonymous", "calls": 1}
    assert second.headers["age"] == "0"


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_protected_route_without_a_credential_is_refused_before_a_hit(
    framework: Framework,
) -> None:
    """The entry the route stored while anonymous never reaches a caller it refuses."""
    # Arrange
    store = SharedStore()
    stored_while_anonymous(framework, store)
    feed = Feed("protected")

    # Act
    with served(framework, "protected", store, feed) as client:
        response = client.get("/feed")

    # Assert
    assert response.status_code == UNAUTHORIZED
    assert "age" not in response.headers
    assert feed.calls == 0


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_route_protected_after_a_restart_never_serves_the_old_entry(
    framework: Framework,
) -> None:
    """A caller the protected route admits is answered by its handler, not the old entry."""
    # Arrange
    store = SharedStore()
    stored_while_anonymous(framework, store)
    feed = Feed("protected")

    # Act
    with served(framework, "protected", store, feed) as client:
        response = client.get("/feed", headers=bearer(token()))

    # Assert
    assert response.status_code == OK
    assert response.json() == {"app": "protected", "calls": 1}
    assert feed.calls == 1


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_scoped_route_caller_lacking_the_scope_is_refused_before_a_hit(
    framework: Framework,
) -> None:
    """A caller without the scope gets `403`, however warm the entry is."""
    # Arrange
    store = SharedStore()
    feed = Feed("scoped")

    # Act
    with served(framework, "scoped", store, feed) as client:
        holder = client.get("/feed", headers=bearer(token(scope=SCOPE)))
        again = client.get("/feed", headers=bearer(token(scope=SCOPE)))
        lacking = client.get("/feed", headers=bearer(token(scope="other")))

    # Assert
    assert holder.status_code == again.status_code == OK
    assert lacking.status_code == FORBIDDEN
    assert "age" not in lacking.headers
    assert feed.calls == (1 if framework == "fastapi" else 2)


def test_cached_responses_protected_route_serves_every_admitted_caller_the_stored_response() -> (
    None
):
    """A route declaring the cache shares one response among the callers it admits."""
    # Arrange
    feed = Feed("protected")

    # Act
    with served("fastapi", "protected", MemoryCacheAdapter(), feed) as client:
        alice = client.get("/feed", headers=bearer(token(sub="alice")))
        bob = client.get("/feed", headers=bearer(token(sub="bob")))

    # Assert
    assert alice.json() == bob.json() == {"app": "protected", "calls": 1}
    assert bob.headers["age"] == "0"
    assert feed.calls == 1


def test_cached_responses_route_given_a_scope_after_a_restart_never_serves_the_old_entry() -> (
    None
):
    """The entry stored for every caller is not read once the route requires a scope."""
    # Arrange
    store = SharedStore()
    with served("fastapi", "protected", store, Feed("protected")) as client:
        client.get("/feed", headers=bearer(token()))
    feed = Feed("scoped")

    # Act
    with served("fastapi", "scoped", store, feed) as client:
        response = client.get("/feed", headers=bearer(token(scope=SCOPE)))

    # Assert
    assert response.json() == {"app": "scoped", "calls": 1}
    assert feed.calls == 1


def test_cached_responses_included_protected_route_never_caches_a_credentialed_read() -> (
    None
):
    """A path `include=` names is cached for a caller without a credential only."""
    # Arrange
    feed = Feed("protected")

    # Act
    with served("starlette", "protected", MemoryCacheAdapter(), feed) as client:
        first = client.get("/feed", headers=bearer(token()))
        second = client.get("/feed", headers=bearer(token()))

    # Assert
    assert first.json() == {"app": "protected", "calls": 1}
    assert second.json() == {"app": "protected", "calls": 2}
    assert "age" not in second.headers


async def _allow(connection: Any, handler: Any) -> None:  # noqa: ANN401
    """Let every caller through, as a guard of the handler's own."""


async def _caller() -> str:
    """Name the caller, as a dependency of the handler's own."""
    return "caller"


@pytest.mark.parametrize("check", ["dependency", "guard"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_cached_responses_litestar_handler_with_checks_of_its_own_is_never_served_a_hit(
    check: str, *, authenticated: bool
) -> None:
    """A check of the handler's own runs on every request, named in `include=` or not."""
    # Arrange
    calls = 0

    @get("/feed", opt=on_litestar.Anonymous(), guards=[_allow])
    async def guarded() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    @get(
        "/feed",
        opt=on_litestar.Anonymous(),
        dependencies={"who": Provide(_caller)},
    )
    async def dependent(who: NamedDependency[str]) -> dict[str, Any]:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app = Litestar([guarded if check == "guard" else dependent])
    uses: list[Any] = [
        ErrorResponses(),
        Cache(MemoryCacheAdapter()),
        CachedResponses(include={"/feed": 60}),
    ]
    if authenticated:
        uses.insert(1, AuthenticatedRequests(verifier()))
    Grelmicro(uses=uses).install(app)

    # Act
    with LitestarClient(app) as client:
        first = client.get("/feed")
        second = client.get("/feed")

    # Assert
    assert first.json() == {"calls": 1}
    assert second.json() == {"calls": 2}
    assert "age" not in second.headers


class Peek:
    """An ASGI app recording whether a gate's declaration reached it."""

    def __init__(self) -> None:
        """Record nothing yet."""
        self.seen: list[bool] = []

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Record what the scope carries, and answer."""
        self.seen.append(DECLARATION_KEY in scope)
        await JSONResponse({})(scope, receive, send)


@pytest.mark.parametrize("cached", [False, True])
def test_route_gate_declaration_never_reaches_what_the_route_runs(
    *, cached: bool
) -> None:
    """A middleware further in never reads the declaration meant for the route."""
    # Arrange
    peek = Peek()
    app = Starlette(routes=[Route("/feed", peek)])
    uses: list[Any] = [ErrorResponses(), AuthenticatedRequests(verifier())]
    if cached:
        uses += [
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=("/feed",)),
        ]
    Grelmicro(uses=uses).install(app)

    # Act
    with StarletteClient(app) as client:
        response = client.get("/feed", headers=bearer(token()))

    # Assert
    assert response.status_code == OK
    assert peek.seen == [False]


def _personal(framework: Framework) -> Any:  # noqa: ANN401
    """Return an app whose anonymous `/home` greets the caller, if any."""
    if framework == "fastapi":
        fastapi_app = FastAPI(openapi_url=None)

        @fastapi_app.get(
            "/home",
            dependencies=[
                on_fastapi.Anonymous(),
                on_fastapi.CachedResponse(ttl=60),
            ],
        )
        async def home(
            principal: on_fastapi.OptionalPrincipal,
        ) -> dict[str, Any]:
            return {"hello": getattr(principal, "subject", None)}

        return fastapi_app
    if framework == "starlette":

        async def greet(request: Request) -> JSONResponse:
            return JSONResponse({"hello": request.headers.get("authorization")})

        return Starlette(routes=[Route("/home", greet)])

    @get("/home", opt=on_litestar.Anonymous())
    async def hello(request: LitestarRequest[Any, Any, Any]) -> dict[str, Any]:
        return {"hello": request.headers.get("authorization")}

    return Litestar([hello])


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_anonymous_route_never_serves_a_credentialed_response(
    framework: Framework,
) -> None:
    """A caller sending a credential to a public route is answered by its handler."""
    # Arrange
    app = _personal(framework)
    Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(
                verifier(),
                exclude=("/home",) if framework == "starlette" else (),
            ),
            Cache(MemoryCacheAdapter()),
            CachedResponses()
            if framework == "fastapi"
            else CachedResponses(include={"/home": 60}),
        ]
    ).install(app)
    client = (
        LitestarClient(app) if framework == "litestar" else StarletteClient(app)
    )

    # Act
    with client:
        alice = client.get("/home", headers=bearer(token(sub="alice")))
        anonymous = client.get("/home")

    # Assert
    assert alice.status_code == anonymous.status_code == OK
    assert anonymous.json() == {"hello": None}


def _per_caller(framework: Framework) -> Any:  # noqa: ANN401
    """Return an app whose protected `/me` answers each caller with their own name.

    FastAPI caches it through a router declaring `CachedResponse()`.
    Starlette and Litestar name it in `include=`.
    """
    if framework == "fastapi":
        router = APIRouter(dependencies=[on_fastapi.CachedResponse(ttl=60)])

        @router.get("/me")
        async def me(principal: on_fastapi.CurrentPrincipal) -> dict[str, Any]:
            return {"me": principal.subject}

        fastapi_app = FastAPI(openapi_url=None)
        fastapi_app.include_router(router)
        return fastapi_app
    if framework == "starlette":

        async def whoami(request: Request) -> JSONResponse:
            return JSONResponse({"me": request.headers.get("authorization")})

        return Starlette(routes=[Route("/me", whoami)])

    @get("/me")
    async def mine(request: LitestarRequest[Any, Any, Any]) -> dict[str, Any]:
        return {"me": request.headers.get("authorization")}

    return Litestar([mine])


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_protected_route_not_shared_never_serves_one_caller_to_another(
    framework: Framework,
) -> None:
    """Without `shared=True`, a caller with a credential is answered by the handler."""
    # Arrange
    app = _per_caller(framework)
    Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(verifier()),
            Cache(MemoryCacheAdapter()),
            CachedResponses()
            if framework == "fastapi"
            else CachedResponses(include={"/me": 60}),
        ]
    ).install(app)
    client = (
        LitestarClient(app) if framework == "litestar" else StarletteClient(app)
    )
    alice_token = token(sub="alice")
    bob_token = token(sub="bob")

    # Act
    with client:
        alice = client.get("/me", headers=bearer(alice_token))
        bob = client.get("/me", headers=bearer(bob_token))

    # Assert
    assert alice.json() != bob.json()


def test_cached_responses_protected_route_declaring_the_cache_without_shared_fails_install() -> (
    None
):
    """A route requiring a caller would never be answered from the cache."""
    # Arrange
    app = FastAPI(openapi_url=None)

    @app.get("/me", dependencies=[on_fastapi.CachedResponse(ttl=60)])
    async def me() -> None:
        """Answer nothing."""  # pragma: no cover

    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(verifier()),
            Cache(MemoryCacheAdapter()),
            CachedResponses(),
        ]
    )

    # Act / Assert
    with pytest.raises(ValueError, match=r"GET /me .*@cached.*shared=True"):
        micro.install(app)


def test_cached_responses_anonymous_route_declaring_shared_fails_install() -> (
    None
):
    """A route serving every caller without a credential has nothing to share."""
    # Arrange
    app = FastAPI(openapi_url=None)

    @app.get(
        "/feed",
        dependencies=[
            on_fastapi.Anonymous(),
            on_fastapi.CachedResponse(ttl=60, shared=True),
        ],
    )
    async def feed() -> None:
        """Answer nothing."""  # pragma: no cover

    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(verifier()),
            Cache(MemoryCacheAdapter()),
            CachedResponses(),
        ]
    )

    # Act / Assert
    with pytest.raises(ValueError, match=r"GET /feed .*shared=True"):
        micro.install(app)


class HeaderCheck:
    """Middleware of a mounted app refusing a request without `x-ok: 1`."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`."""
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Refuse `403` without the header, else pass the request on."""
        if (b"x-ok", b"1") not in scope["headers"]:
            await JSONResponse({"denied": True}, status_code=FORBIDDEN)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


def _checked_sub(framework: str, *, gated: bool, declared: bool) -> Any:  # noqa: ANN401
    """Return an app mounting at `/sub` an app whose middleware checks `x-ok`.

    Its `/s` counts the calls it served in `calls`.
    """
    calls = Feed("sub")
    if framework == "starlette":

        async def read(request: Request) -> JSONResponse:  # noqa: ARG001
            return JSONResponse(calls.serve())

        sub: Any = Starlette(
            routes=[Route("/s", read)], middleware=[Middleware(HeaderCheck)]
        )
    else:
        sub = FastAPI(openapi_url=None, middleware=[Middleware(HeaderCheck)])
        dependencies: list[Any] = [on_fastapi.Anonymous()] if gated else []
        if declared:
            dependencies.append(on_fastapi.CachedResponse(ttl=60))

        @sub.get("/s", dependencies=dependencies)
        async def fastapi_read() -> dict[str, Any]:
            return calls.serve()

    app = Starlette(routes=[Mount("/sub", app=sub)])
    app.state.calls = calls
    return app


@pytest.mark.parametrize("gated", [False, True])
@pytest.mark.parametrize(
    ("framework", "declared"),
    [("starlette", False), ("fastapi", False), ("fastapi", True)],
)
def test_cached_responses_mounted_app_middleware_runs_before_every_hit(
    framework: str, *, declared: bool, gated: bool
) -> None:
    """A request the mounted app's middleware refuses is never served the entry."""
    # Arrange
    app = _checked_sub(framework, gated=gated, declared=declared)
    uses: list[Any] = [
        ErrorResponses(),
        Cache(MemoryCacheAdapter()),
        CachedResponses()
        if declared
        else CachedResponses(include={"/sub/*": 60}),
    ]
    if gated:
        excluded = ("/sub/*",) if framework == "starlette" else ()
        uses.insert(1, AuthenticatedRequests(verifier(), exclude=excluded))
    Grelmicro(uses=uses).install(app)

    # Act
    with StarletteClient(app) as client:
        allowed = client.get("/sub/s", headers={"x-ok": "1"})
        refused = client.get("/sub/s")

    # Assert
    assert allowed.status_code == OK
    assert refused.status_code in {UNAUTHORIZED, FORBIDDEN}
    assert app.state.calls.calls == 1


def test_cached_responses_nested_caches_answer_one_age() -> None:
    """A cache storing what another answered keeps none of its `Age`."""
    # Arrange
    feed = Feed("nested")

    async def read(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(feed.serve())

    inner = CachedResponsesMiddleware(
        Starlette(routes=[Route("/feed", read)]),
        cache=TTLCache(
            backend=MemoryCacheAdapter(), serializer=JsonSerializer()
        ),
        include={"/feed": 60},
    )
    outer = CachedResponsesMiddleware(
        inner,
        cache=TTLCache(
            backend=MemoryCacheAdapter(), serializer=JsonSerializer()
        ),
        include={"/feed": 60},
    )

    # Act
    with StarletteClient(outer) as client:
        client.get("/feed")
        hit = client.get("/feed")

    # Assert
    assert hit.headers.get_list("age") == ["0"]
    assert feed.calls == 1


def test_cached_responses_shared_route_leaves_a_request_with_a_cookie_to_its_handler() -> (
    None
):
    """A cookie no gate reads keeps the request out of a shared entry too."""
    # Arrange
    feed = Feed("protected")

    # Act
    with served("fastapi", "protected", MemoryCacheAdapter(), feed) as client:
        client.get("/feed", headers=bearer(token()))
        with_cookie = client.get(
            "/feed", headers={**bearer(token()), "cookie": "session=s"}
        )

    # Assert
    assert with_cookie.json() == {"app": "protected", "calls": 2}
    assert feed.calls == 2  # noqa: PLR2004


def _authenticating() -> Grelmicro:
    """Return a `Grelmicro` authenticating an app and caching its responses."""
    return Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(verifier()),
            Cache(MemoryCacheAdapter()),
            CachedResponses(),
        ]
    )


@pytest.mark.parametrize("where", ["router", "include", "app"])
def test_cached_responses_router_declaring_shared_fails_install(
    where: str,
) -> None:
    """Only a route may say its own response is the same for every caller."""
    # Arrange
    shared = [on_fastapi.CachedResponse(ttl=60, shared=True)]
    router = APIRouter(
        prefix="/v1", dependencies=shared if where == "router" else []
    )

    @router.get("/me")
    async def me(principal: on_fastapi.CurrentPrincipal) -> dict[str, Any]:
        return {"me": principal.subject}  # pragma: no cover

    app = FastAPI(
        openapi_url=None, dependencies=shared if where == "app" else []
    )
    app.include_router(
        router, dependencies=shared if where == "include" else []
    )

    # Act / Assert
    with pytest.raises(
        ValueError, match=r"/v1/me.*CachedResponse\(shared=True\)"
    ):
        _authenticating().install(app)


def test_cached_responses_route_landing_under_a_shared_router_is_refused() -> (
    None
):
    """A route added after install under such a router fails as it lands."""
    # Arrange
    router = APIRouter(
        prefix="/v1",
        dependencies=[on_fastapi.CachedResponse(ttl=60, shared=True)],
    )
    app = FastAPI(openapi_url=None)
    app.include_router(router)
    _authenticating().install(app)

    async def me(principal: on_fastapi.CurrentPrincipal) -> dict[str, Any]:
        return {"me": principal.subject}  # pragma: no cover

    # Act / Assert
    with pytest.raises(ValueError, match=r"CachedResponse\(shared=True\)"):
        app.add_api_route("/v1/me", me, dependencies=router.dependencies)


@pytest.mark.parametrize(
    "reader",
    [
        on_fastapi.CurrentPrincipal,
        on_fastapi.OptionalPrincipal,
        on_fastapi.Claims,
        on_fastapi.CurrentToken,
        Annotated[Principal, on_fastapi.Authenticated()],
    ],
)
def test_cached_responses_shared_route_reading_the_caller_fails_install(
    reader: Any,  # noqa: ANN401
) -> None:
    """A handler taking the caller answers each caller its own response."""
    # Arrange
    app = FastAPI(openapi_url=None)

    async def me(caller: object) -> None:
        """Answer nothing."""  # pragma: no cover

    me.__annotations__ = {"caller": reader, "return": None}

    app.add_api_route(
        "/me",
        me,
        methods=["GET"],
        dependencies=[on_fastapi.CachedResponse(ttl=60, shared=True)],
    )

    # Act / Assert
    with pytest.raises(ValueError, match=r"GET /me .*'caller'"):
        _authenticating().install(app)
