"""Tests for the response cache: what it answers, and what it refuses to keep."""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from inspect import iscoroutinefunction
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, Response, Security
from fastapi.security import APIKeyHeader
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse
from starlette.routing import Mount, Route, Router

from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.errors import SettingsValidationError
from grelmicro.http import (
    CachedResponses,
    CachedResponsesMiddleware,
    StoredResponse,
)
from grelmicro.http._response_cache import (
    _UNSTORABLE_LIMIT,
    _WARNED_LIMIT,
    _declared_schemes,
    _routing_dependencies,
    declare_cached,
)
from grelmicro.integrations.fastapi import CachedResponse

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Iterator,
        MutableMapping,
        Sequence,
    )

pytestmark = [pytest.mark.timeout(5)]

HTTP_200_OK = 200
HTTP_304_NOT_MODIFIED = 304
HTTP_401_UNAUTHORIZED = 401
HTTP_404_NOT_FOUND = 404
TTL = 60.0
BIG = 2048
TWICE = 2
OTHER_TTL = 300.0
READS = 3
SHARED_TTL = 50.0
"""How many times the handler runs when nothing was stored."""


class _RoutingProxy:
    """ASGI middleware exposing the routes of the application it wraps."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self.app = app

    @property
    def routes(self) -> Any:  # noqa: ANN401
        """Forward route introspection like a transparent middleware."""
        return self.app.routes

    async def __call__(
        self,
        scope: Any,  # noqa: ANN401
        receive: Any,  # noqa: ANN401
        send: Any,  # noqa: ANN401
    ) -> None:
        """Pass the request through."""
        await self.app(scope, receive, send)


def _app(component: CachedResponses) -> FastAPI:
    """Return an app whose `/reads` route counts what reached the handler."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), component])
    app = FastAPI()
    app.state.calls = 0
    micro.install(app)

    @app.get("/reads", dependencies=[CachedResponse(ttl=TTL)])
    async def reads() -> dict[str, int]:
        app.state.calls += 1
        return {"calls": app.state.calls}

    @app.get("/live")
    async def live() -> dict[str, int]:
        app.state.calls += 1
        return {"calls": app.state.calls}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Serve an app carrying the component."""
    with TestClient(_app(CachedResponses())) as test_client:
        yield test_client


# --- What it answers ---


def test_a_marked_route_is_answered_from_the_cache(
    client: TestClient,
) -> None:
    """The second read never reaches the handler."""
    # Act
    first = client.get("/reads")
    second = client.get("/reads")

    # Assert
    assert first.json() == second.json() == {"calls": 1}
    assert "age" in second.headers


def test_a_hit_says_how_long_it_has_been_kept(client: TestClient) -> None:
    """`Age` is what a cache tells a client instead of a proprietary header."""
    # Arrange
    client.get("/reads")

    # Act
    response = client.get("/reads")

    # Assert
    assert response.headers["age"] == "0"


def test_an_unmarked_route_is_never_cached(client: TestClient) -> None:
    """Nothing is cached until a route asks to be."""
    # Act
    first = client.get("/live")
    second = client.get("/live")

    # Assert
    assert first.json() != second.json()


def test_a_stored_response_carries_an_entity_tag(client: TestClient) -> None:
    """A cache that answers `304` needs a tag, so it adds one."""
    # Act
    response = client.get("/reads")

    # Assert
    assert response.headers["etag"].startswith('"')


def test_a_client_holding_the_tag_is_answered_304(
    client: TestClient,
) -> None:
    """The body is what a `304` saves, and the cache has it to save."""
    # Arrange
    tag = client.get("/reads").headers["etag"]

    # Act
    response = client.get("/reads", headers={"If-None-Match": tag})

    # Assert
    assert response.status_code == HTTP_304_NOT_MODIFIED
    assert response.content == b""
    assert response.headers["etag"] == tag


def test_a_head_is_answered_from_the_read_that_was_stored(
    client: TestClient,
) -> None:
    """A `HEAD` carries the headers of the `GET`, and none of its body."""
    # Arrange
    client.get("/reads")

    # Act
    response = client.head("/reads")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert response.content == b""
    assert "age" in response.headers


def test_a_head_never_fills_the_cache() -> None:
    """A `HEAD` body is empty, and would answer the `GET` after it with nothing."""
    # Arrange
    calls = 0

    async def handler(request: Any) -> Response:  # noqa: ANN401, ARG001
        nonlocal calls
        calls += 1
        return Response(b"ok")

    app = Starlette(routes=[Route("/reads", handler)])
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/reads": TTL}),
        ]
    )
    micro.install(app)

    # Act
    with TestClient(app) as client:
        head = client.head("/reads")
        read = client.get("/reads")

    # Assert
    assert head.status_code == HTTP_200_OK
    assert calls == TWICE
    assert read.content == b"ok"


def test_a_marked_route_takes_the_components_ttl_when_it_names_none() -> None:
    """`CachedResponse()` with no `ttl` is the component's."""
    # Arrange
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), CachedResponses(ttl=TTL)]
    )
    app = FastAPI()
    calls = 0
    micro.install(app)

    @app.get("/reads", dependencies=[CachedResponse()])
    async def reads() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    # Act
    with TestClient(app) as client:
        client.get("/reads")
        client.get("/reads")

    # Assert
    assert calls == 1


# --- What never reads the cache ---


@pytest.mark.parametrize(
    "header", ["Authorization", "Cookie"], ids=["authorization", "cookie"]
)
def test_a_credentialed_request_neither_reads_nor_fills(
    client: TestClient, header: str
) -> None:
    """A response authorized for one caller is never handed to another."""
    # Act
    first = client.get("/reads", headers={header: "secret"})
    second = client.get("/reads", headers={header: "secret"})

    # Assert
    assert first.json() != second.json()


def test_component_cache_treats_starlette_authentication_as_private() -> None:
    """App-level authentication keeps every caller out of the shared cache."""

    class HeaderAuthentication(AuthenticationBackend):
        async def authenticate(
            self,
            conn: Any,  # noqa: ANN401
        ) -> tuple[AuthCredentials, SimpleUser] | None:
            identity = conn.headers.get("x-api-key")
            if identity is None:
                return None
            return AuthCredentials(["authenticated"]), SimpleUser(identity)

    calls = 0

    async def private(request: Request) -> Response:
        nonlocal calls
        if not request.user.is_authenticated:
            return Response(status_code=HTTP_401_UNAUTHORIZED)
        calls += 1
        return Response(f"{request.user.display_name}:{calls}")

    app = Starlette(
        routes=[Route("/private", private)],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=HeaderAuthentication(),
            )
        ],
    )
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/private": TTL}),
        ]
    )
    micro.install(app)

    with TestClient(app) as test_client:
        alice = test_client.get("/private", headers={"X-API-Key": "alice"})
        bob = test_client.get("/private", headers={"X-API-Key": "bob"})
        anonymous = test_client.get("/private")

    assert alice.text == "alice:1"
    assert bob.text == "bob:2"
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert all(
        "age" not in response.headers for response in (alice, bob, anonymous)
    )
    assert calls == TWICE


def test_component_cache_treats_fastapi_authentication_as_private() -> None:
    """A marked FastAPI route retains app-level authentication metadata."""

    class HeaderAuthentication(AuthenticationBackend):
        async def authenticate(
            self,
            conn: Any,  # noqa: ANN401
        ) -> tuple[AuthCredentials, SimpleUser] | None:
            identity = conn.headers.get("x-api-key")
            if identity is None:
                return None
            return AuthCredentials(["authenticated"]), SimpleUser(identity)

    app = FastAPI(
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=HeaderAuthentication(),
            )
        ]
    )
    calls = 0

    @app.get("/private", dependencies=[CachedResponse(ttl=TTL)])
    async def private(request: Request) -> Response:
        nonlocal calls
        if not request.user.is_authenticated:
            return Response(status_code=HTTP_401_UNAUTHORIZED)
        calls += 1
        return Response(f"{request.user.display_name}:{calls}")

    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    micro.install(app)

    with TestClient(app) as test_client:
        alice = test_client.get("/private", headers={"X-API-Key": "alice"})
        bob = test_client.get("/private", headers={"X-API-Key": "bob"})
        anonymous = test_client.get("/private")

    assert alice.text == "alice:1"
    assert bob.text == "bob:2"
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert all(
        "age" not in response.headers for response in (alice, bob, anonymous)
    )
    assert calls == TWICE


def test_component_cache_keeps_public_routes_under_optional_auth_cacheable() -> (
    None
):
    """An anonymous public response is not private merely because auth ran."""

    class OptionalAuthentication(AuthenticationBackend):
        async def authenticate(
            self,
            conn: Any,  # noqa: ANN401
        ) -> tuple[AuthCredentials, SimpleUser] | None:
            identity = conn.headers.get("x-api-key")
            if identity is None:
                return None
            return AuthCredentials(["authenticated"]), SimpleUser(identity)

    calls = 0

    async def public(_request: Request) -> Response:
        nonlocal calls
        calls += 1
        return Response(str(calls))

    app = Starlette(
        routes=[Route("/public", public)],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=OptionalAuthentication(),
            )
        ],
    )
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/public": TTL}),
        ]
    )
    micro.install(app)

    with TestClient(app) as test_client:
        first = test_client.get("/public")
        replayed = test_client.get("/public")

    assert first.text == replayed.text == "1"
    assert replayed.headers["age"] == "0"
    assert calls == 1


def test_the_requests_own_cache_control_is_not_read(
    client: TestClient,
) -> None:
    """A caller that could ask for the handler could spend it at will."""
    # Arrange
    client.get("/reads")

    # Act
    no_store = client.get("/reads", headers={"Cache-Control": "no-store"})
    no_cache = client.get("/reads", headers={"Cache-Control": "no-cache"})

    # Assert
    assert no_store.json() == no_cache.json() == {"calls": 1}


def test_a_path_that_is_excluded_is_never_cached() -> None:
    """`exclude` wins over the mark the route carries."""
    # Arrange
    app = _app(CachedResponses(exclude=("/reads",)))

    # Act
    with TestClient(app) as client:
        first = client.get("/reads")
        second = client.get("/reads")

    # Assert
    assert first.json() != second.json()


def test_a_websocket_scope_passes_through() -> None:
    """A response cache is about responses, and answers nothing else."""
    # Arrange
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        seen.append(scope["type"])

    middleware = CachedResponsesMiddleware(app, cache=_cache())

    # Act
    anyio.run(middleware, {"type": "websocket"}, _receive, _nowhere)

    # Assert
    assert seen == ["websocket"]


# --- Naming paths instead of routes ---


def test_paths_cache_a_route_that_carries_no_mark() -> None:
    """A router whose handlers you cannot mark is named by its URL."""
    # Arrange
    app = _app(CachedResponses(include={"/live": TTL}))

    # Act
    with TestClient(app) as client:
        first = client.get("/live")
        second = client.get("/live")

    # Assert
    assert first.json() == second.json()


def test_a_prefix_pattern_covers_the_router_under_it() -> None:
    """`"/x/*"` is the same matching every grelmicro middleware uses."""
    # Arrange
    calls = 0

    async def handler(request: Any) -> Response:  # noqa: ANN401, ARG001
        nonlocal calls
        calls += 1
        return Response(b"ok")

    app = Starlette(routes=[Route("/shop/items", handler)])
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/shop/*": TTL}),
        ]
    )
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/shop/items")
        client.get("/shop/items")

    # Assert
    assert calls == 1


def test_a_mounted_route_is_read_with_its_prefix() -> None:
    """A rule under a mount names the path the request actually asks for."""
    # Arrange
    calls = 0
    inner = FastAPI()

    @inner.get("/items", dependencies=[CachedResponse(ttl=TTL)])
    async def items() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app = FastAPI()
    app.mount("/shop", inner)
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/shop/items")
        client.get("/shop/items")

    # Assert
    assert calls == 1


def test_a_mounted_middleware_keeps_its_route_declarations_inside() -> None:
    """A parent cache cannot answer before a mounted middleware runs."""
    # Arrange
    calls = 0
    inner = FastAPI()

    @inner.get("/items", dependencies=[CachedResponse(ttl=TTL)])
    async def items() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app = FastAPI()
    app.mount(
        "/shop",
        CORSMiddleware(
            inner,
            allow_origins=["https://client.example"],
        ),
    )
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        first = client.get(
            "/shop/items", headers={"Origin": "https://client.example"}
        )
        second = client.get(
            "/shop/items", headers={"Origin": "https://client.example"}
        )

    # Assert
    assert first.json() == {"calls": 1}
    assert second.json() == {"calls": 2}
    assert second.headers["access-control-allow-origin"] == (
        "https://client.example"
    )


def test_route_middleware_refuses_an_explicit_parent_cache_rule() -> None:
    """A parent cache cannot answer before route-local authentication."""

    # Arrange
    class HeaderAuthentication(AuthenticationBackend):
        async def authenticate(
            self,
            conn: Any,  # noqa: ANN401
        ) -> tuple[AuthCredentials, SimpleUser] | None:
            identity = conn.headers.get("x-api-key")
            if identity is None:
                return None
            return AuthCredentials(["authenticated"]), SimpleUser(identity)

    calls = 0

    async def private(request: Request) -> Response:
        nonlocal calls
        calls += 1
        identity = (
            request.user.display_name
            if request.user.is_authenticated
            else "anonymous"
        )
        return Response(f"{identity}:{calls}")

    app = Starlette(
        routes=[
            Route(
                "/private",
                private,
                middleware=[
                    Middleware(
                        AuthenticationMiddleware,
                        backend=HeaderAuthentication(),
                    )
                ],
            )
        ]
    )
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/private": TTL}),
        ]
    )
    micro.install(app)

    # Act
    with TestClient(app) as client:
        alice = client.get("/private", headers={"X-API-Key": "alice"})
        bob = client.get("/private", headers={"X-API-Key": "bob"})
        anonymous = client.get("/private")

    # Assert
    assert alice.text == "alice:1"
    assert bob.text == "bob:2"
    assert anonymous.text == "anonymous:3"


def test_hand_wired_cache_reads_inner_application_middleware() -> None:
    """Direct wrapping cannot answer before the app authenticates a caller."""

    # Arrange
    class HeaderAuthentication(AuthenticationBackend):
        async def authenticate(
            self,
            conn: Any,  # noqa: ANN401
        ) -> tuple[AuthCredentials, SimpleUser] | None:
            identity = conn.headers.get("x-api-key")
            if identity is None:
                return None
            return AuthCredentials(["authenticated"]), SimpleUser(identity)

    calls = 0

    async def private(request: Request) -> Response:
        nonlocal calls
        if not request.user.is_authenticated:
            return Response(status_code=HTTP_401_UNAUTHORIZED)
        calls += 1
        return Response(f"{request.user.display_name}:{calls}")

    app = Starlette(
        routes=[Route("/private", private)],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=HeaderAuthentication(),
            )
        ],
    )
    wrapped = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/private": TTL},
    )

    # Act
    with TestClient(wrapped) as client:
        alice = client.get("/private", headers={"X-API-Key": "alice"})
        bob = client.get("/private", headers={"X-API-Key": "bob"})
        anonymous = client.get("/private")

    # Assert
    assert alice.text == "alice:1"
    assert bob.text == "bob:2"
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert "age" not in bob.headers
    assert calls == TWICE


@pytest.mark.parametrize("entrypoint", ["route", "bound-router"])
def test_hand_wired_cache_normalizes_starlette_entrypoints(
    entrypoint: str,
) -> None:
    """A direct Route and bound Router endpoint retain route authentication."""

    class HeaderAuthentication(AuthenticationBackend):
        async def authenticate(
            self,
            conn: Any,  # noqa: ANN401
        ) -> tuple[AuthCredentials, SimpleUser] | None:
            identity = conn.headers.get("x-api-key")
            if identity is None:
                return None
            return AuthCredentials(["authenticated"]), SimpleUser(identity)

    calls = 0

    async def private(request: Request) -> Response:
        nonlocal calls
        if not request.user.is_authenticated:
            return Response(status_code=HTTP_401_UNAUTHORIZED)
        calls += 1
        return Response(f"{request.user.display_name}:{calls}")

    route = Route(
        "/private",
        private,
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=HeaderAuthentication(),
            )
        ],
    )
    router = Router(routes=[route])
    app = route if entrypoint == "route" else router.app
    wrapped = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/private": TTL},
    )

    client = TestClient(wrapped)
    alice = client.get("/private", headers={"X-API-Key": "alice"})
    bob = client.get("/private", headers={"X-API-Key": "bob"})
    anonymous = client.get("/private")

    assert alice.text == "alice:1"
    assert bob.text == "bob:2"
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert "age" not in bob.headers
    assert calls == TWICE


@pytest.mark.parametrize("entrypoint", ["route", "bound-router"])
def test_hand_wired_public_starlette_entrypoints_remain_cacheable(
    entrypoint: str,
) -> None:
    """Normalizing a routing entry point does not invent a private boundary."""
    calls = 0

    async def public(_request: Request) -> Response:
        nonlocal calls
        calls += 1
        return Response(str(calls))

    route = Route("/public", public)
    router = Router(routes=[route])
    app = route if entrypoint == "route" else router.app
    wrapped = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/public": TTL},
    )

    client = TestClient(wrapped)
    first = client.get("/public")
    replayed = client.get("/public")

    assert first.text == replayed.text == "1"
    assert replayed.headers["age"] == "0"
    assert calls == 1


def test_add_middleware_cache_ignores_builtin_exception_routing() -> None:
    """Starlette's internal exception router is not a user auth boundary."""
    calls = 0

    async def public(_request: Request) -> Response:
        nonlocal calls
        calls += 1
        return Response(str(calls))

    app = Starlette(routes=[Route("/public", public)])
    app.add_middleware(
        CachedResponsesMiddleware,
        cache=_cache(),
        include={"/public": TTL},
    )

    with TestClient(app) as client:
        first = client.get("/public")
        replayed = client.get("/public")

    assert first.text == replayed.text == "1"
    assert replayed.headers["age"] == "0"
    assert calls == 1


def test_leaf_router_authentication_is_an_exact_cache_boundary() -> None:
    """A Router used as a Route endpoint cannot hide route authentication."""

    # Arrange
    class HeaderAuthentication(AuthenticationBackend):
        async def authenticate(
            self,
            conn: Any,  # noqa: ANN401
        ) -> tuple[AuthCredentials, SimpleUser] | None:
            identity = conn.headers.get("x-api-key")
            if identity is None:
                return None
            return AuthCredentials(["authenticated"]), SimpleUser(identity)

    calls = 0

    async def private(request: Request) -> Response:
        nonlocal calls
        if not request.user.is_authenticated:
            return Response(status_code=HTTP_401_UNAUTHORIZED)
        calls += 1
        return Response(f"{request.user.display_name}:{calls}")

    inner = Router(
        routes=[
            Route(
                "/private",
                private,
                middleware=[
                    Middleware(
                        AuthenticationMiddleware,
                        backend=HeaderAuthentication(),
                    )
                ],
            )
        ]
    )
    app = Router(routes=[Route("/private", inner)])
    wrapped = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/private": TTL},
    )

    # Act
    with TestClient(wrapped) as client:
        alice = client.get("/private", headers={"X-API-Key": "alice"})
        bob = client.get("/private", headers={"X-API-Key": "bob"})
        anonymous = client.get("/private")

    # Assert
    assert alice.text == "alice:1"
    assert bob.text == "bob:2"
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert "age" not in bob.headers
    assert calls == TWICE


def test_leaf_fastapi_dependency_is_an_exact_cache_boundary() -> None:
    """A leaf FastAPI router cannot hide a dependency from a parent cache."""
    # Arrange
    calls = 0
    inner = FastAPI()

    async def authenticate(request: Request) -> None:
        if request.headers.get("x-api-key") is None:
            from fastapi import HTTPException  # noqa: PLC0415

            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED)

    @inner.get("/private", dependencies=[Depends(authenticate)])
    async def private(request: Request) -> Response:
        nonlocal calls
        calls += 1
        return Response(f"{request.headers['x-api-key']}:{calls}")

    middle = Router(routes=[Route("/private", inner)])
    app = Router(routes=[Route("/private", middle)])
    wrapped = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/private": TTL},
    )

    # Act
    with TestClient(wrapped) as client:
        alice = client.get("/private", headers={"X-API-Key": "alice"})
        bob = client.get("/private", headers={"X-API-Key": "bob"})
        anonymous = client.get("/private")

    # Assert
    assert alice.text == "alice:1"
    assert bob.text == "bob:2"
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert "age" not in bob.headers
    assert calls == TWICE


def test_public_leaf_router_remains_cacheable() -> None:
    """Only a protected leaf router makes its exact outer route a boundary."""
    # Arrange
    calls = 0

    async def public(_request: Request) -> Response:
        nonlocal calls
        calls += 1
        return Response(str(calls))

    inner = FastAPI()
    inner.add_api_route("/public", public, methods=["GET"])
    app = Router(routes=[Route("/public", inner)])
    wrapped = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/public": TTL},
    )

    # Act
    with TestClient(wrapped) as client:
        first = client.get("/public")
        replayed = client.get("/public")

    # Assert
    assert first.text == replayed.text == "1"
    assert "age" in replayed.headers
    assert calls == 1
    assert not _routing_dependencies(None, "GET")


@pytest.mark.parametrize(
    "configuration",
    ["constructor", "add-middleware", "router"],
)
@pytest.mark.parametrize("policy", ["declaration", "include"])
def test_middleware_configured_inside_a_mount_is_a_cache_boundary(
    configuration: str,
    policy: str,
) -> None:
    """A parent cache cannot answer before lazily built child middleware."""

    # Arrange
    class HeaderAuthentication(AuthenticationBackend):
        async def authenticate(
            self,
            conn: Any,  # noqa: ANN401
        ) -> tuple[AuthCredentials, SimpleUser] | None:
            identity = conn.headers.get("x-api-key")
            if identity is None:
                return None
            return AuthCredentials(["authenticated"]), SimpleUser(identity)

    middleware = [
        Middleware(AuthenticationMiddleware, backend=HeaderAuthentication())
    ]
    inner = FastAPI(
        middleware=middleware if configuration == "constructor" else None
    )
    calls = 0

    dependencies = (
        [CachedResponse(ttl=TTL)] if policy == "declaration" else None
    )

    @inner.get("/items", dependencies=dependencies)
    async def items(request: Request) -> dict[str, str | int]:
        nonlocal calls
        calls += 1
        identity = (
            request.user.display_name
            if request.user.is_authenticated
            else "anonymous"
        )
        return {"user": identity, "calls": calls}

    mounted: Any = inner
    if configuration == "add-middleware":
        inner.add_middleware(
            AuthenticationMiddleware, backend=HeaderAuthentication()
        )
    elif configuration == "router":
        mounted = Router(routes=inner.router.routes, middleware=middleware)

    app = FastAPI()
    app.mount("/shop", mounted)
    cached = (
        CachedResponses()
        if policy == "declaration"
        else CachedResponses(include={"/shop/items": TTL})
    )
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), cached])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        alice = client.get("/shop/items", headers={"X-API-Key": "alice"})
        bob = client.get("/shop/items", headers={"X-API-Key": "bob"})
        anonymous = client.get("/shop/items")

    # Assert
    assert alice.json() == {"user": "alice", "calls": 1}
    assert bob.json() == {"user": "bob", "calls": 2}
    assert anonymous.json() == {"user": "anonymous", "calls": 3}


@pytest.mark.parametrize("policy", ["declaration", "include"])
def test_route_transparent_mounted_wrapper_is_a_cache_boundary(
    policy: str,
) -> None:
    """Forwarded route attributes cannot hide an explicit ASGI boundary."""
    # Arrange
    calls = 0
    inner = FastAPI()

    dependencies = (
        [CachedResponse(ttl=TTL)] if policy == "declaration" else None
    )

    @inner.get("/items", dependencies=dependencies)
    async def items() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app = FastAPI()
    app.mount("/shop", _RoutingProxy(inner))
    cached = (
        CachedResponses()
        if policy == "declaration"
        else CachedResponses(include={"/shop/items": TTL})
    )
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), cached])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        first = client.get("/shop/items")
        second = client.get("/shop/items")

    # Assert
    assert first.json() == {"calls": 1}
    assert second.json() == {"calls": 2}


def test_a_route_with_other_dependencies_is_read_too() -> None:
    """The declaration sits beside whatever else the route depends on."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    micro.install(app)
    calls = 0

    async def audit() -> None:
        """Stand in for the gate a route already declares."""

    @app.get(
        "/reads",
        dependencies=[Depends(audit), CachedResponse(ttl=TTL)],
    )
    async def reads() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    # Act
    with TestClient(app) as client:
        client.get("/reads")
        client.get("/reads")

    # Assert
    assert calls == 1


def test_a_declaration_on_a_write_is_refused_where_it_is_written() -> None:
    """A method that changes something reaches the handler every time."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()

    @app.post("/orders", dependencies=[CachedResponse(ttl=TTL)])
    async def create() -> dict[str, int]:
        return {"id": 1}

    # Act / Assert
    with pytest.raises(TypeError, match="answers POST"):
        micro.install(app)


def test_a_route_added_after_install_is_read_when_the_app_starts() -> None:
    """`install` goes before the routes, and the app start catches up."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    micro.install(app)
    calls = 0

    @app.get("/late", dependencies=[CachedResponse(ttl=TTL)])
    async def late() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    # Act
    with TestClient(app) as client:
        client.get("/late")
        client.get("/late")

    # Assert
    assert calls == 1


def test_hand_wired_cache_revalidates_routes_added_after_a_request() -> None:
    """A late FastAPI gate cannot inherit stale hand-wired cache policy."""
    app = FastAPI()

    @app.get("/public")
    async def public() -> dict[str, bool]:
        return {"public": True}

    wrapped = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/private": TTL},
    )
    api_key = APIKeyHeader(name="X-API-Key")

    async def require_key(
        key: str = Security(api_key),
    ) -> str:
        return key

    async def private() -> dict[str, bool]:
        return {"private": True}

    with TestClient(wrapped) as client:
        assert client.get("/public").status_code == HTTP_200_OK
        app.add_api_route(
            "/private",
            private,
            methods=["GET"],
            dependencies=[Depends(require_key)],
        )
        with pytest.raises(TypeError, match="gated by APIKeyHeader"):
            client.get("/private", headers={"X-API-Key": "alice"})


def test_hand_wired_cache_revalidates_a_lifespan_added_route() -> None:
    """A route created during startup is validated before its first cache read."""
    api_key = APIKeyHeader(name="X-API-Key")

    async def require_key(
        key: str = Security(api_key),
    ) -> str:
        return key

    async def private() -> dict[str, bool]:
        return {"private": True}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.add_api_route(
            "/private",
            private,
            methods=["GET"],
            dependencies=[Depends(require_key)],
        )
        yield

    app = FastAPI(lifespan=lifespan)
    wrapped = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/private": TTL},
    )

    with (
        TestClient(wrapped) as client,
        pytest.raises(TypeError, match="gated by APIKeyHeader"),
    ):
        client.get("/private", headers={"X-API-Key": "alice"})


# --- What is not stored ---


async def _nothing(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
    """Stand in for an app the test never reaches."""


async def _receive() -> MutableMapping[str, Any]:
    """Stand in for the body a read never sends."""
    return {"type": "http.request", "body": b"", "more_body": False}


async def _nowhere(message: MutableMapping[str, Any]) -> None:
    """Take a response nowhere."""


def _split(value: str) -> list[str]:
    """Return one comma-separated header value as its parts."""
    return [part.strip().lower() for part in value.split(",")]


class _LosingLock:
    """A lock that fails on its way out, after the handler answered."""

    async def __aenter__(self) -> None:
        """Take it."""

    async def __aexit__(self, *args: object) -> None:
        """Refuse to give it back."""
        msg = "the lease is gone"
        raise ConnectionError(msg)


class _LosingGuard:
    """A stampede guard whose lock refuses to be let go of."""

    async def get_lock(self, key: str) -> _LosingLock:  # noqa: ARG002
        """Return a lock that raises where the caller lets go of it."""
        return _LosingLock()


def _read_scope() -> MutableMapping[str, Any]:
    """Return the scope of a plain read of `/reads`."""
    return {
        "type": "http",
        "method": "GET",
        "path": "/reads",
        "headers": [],
        "query_string": b"",
    }


def test_nonempty_auth_scopes_make_a_request_private() -> None:
    """Authentication scopes bypass the cache even without an authenticated user."""
    calls = 0

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        nonlocal calls
        calls += 1
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = CachedResponsesMiddleware(
        app,
        cache=_cache(),
        include={"/reads": TTL},
    )
    scope = _read_scope()
    scope["user"] = SimpleNamespace(is_authenticated=False)
    scope["auth"] = SimpleNamespace(scopes=["authenticated"])

    async def scenario() -> None:
        await middleware(scope, _receive, _nowhere)
        await middleware(dict(scope), _receive, _nowhere)

    anyio.run(scenario)

    assert calls == TWICE


def _cache() -> TTLCache[Any]:
    """Return a cache over a backend of this test's own."""
    return TTLCache(
        ttl=TTL, backend=MemoryCacheAdapter(), serializer=JsonSerializer()
    )


def _ran_twice(headers: list[tuple[bytes, bytes]]) -> int:
    """Return how many times the app ran for two identical reads."""
    calls = 0

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        nonlocal calls
        calls += 1
        await send(
            {"type": "http.response.start", "status": 200, "headers": headers}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: MutableMapping[str, Any]) -> None:
        """Take the response nowhere. What is measured is the app running."""

    async def scenario() -> None:
        async with MemoryCacheAdapter() as backend:
            middleware = CachedResponsesMiddleware(
                app,
                cache=TTLCache(
                    ttl=TTL, backend=backend, serializer=JsonSerializer()
                ),
                include={"/reads": TTL},
            )
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/reads",
                "headers": [],
                "query_string": b"",
            }
            await middleware(scope, _receive, send)
            await middleware(dict(scope), _receive, send)

    anyio.run(scenario)
    return calls


class _BrokenStore(MemoryCacheAdapter):
    """A backend that fails the one operation it is asked to fail."""

    def __init__(self, failing: str) -> None:
        """Take the name of the operation that raises."""
        super().__init__()
        self._failing = failing

    async def get(self, *, key: str) -> bytes | None:
        """Read, or refuse to."""
        if self._failing == "get":
            msg = "the store is down"
            raise ConnectionError(msg)
        return await super().get(key=key)

    async def set(
        self,
        *,
        key: str,
        value: bytes,
        ttl: float,
        tags: Sequence[str] = (),
    ) -> None:
        """Write, or refuse to."""
        if self._failing == "set":
            msg = "the store is down"
            raise ConnectionError(msg)
        await super().set(key=key, value=value, ttl=ttl, tags=tags)


def _through_a_broken_store(failing: str, reads: int = 1) -> list[Any]:
    """Return the status and the body a read is answered with anyway."""
    answered: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: MutableMapping[str, Any]) -> None:
        answered.append(message.get("status", message.get("body")))

    async def scenario() -> None:
        async with _BrokenStore(failing) as backend:
            middleware = CachedResponsesMiddleware(
                app,
                cache=TTLCache(
                    ttl=TTL, backend=backend, serializer=JsonSerializer()
                ),
                include={"/reads": TTL},
            )
            for _ in range(reads):
                await middleware(_read_scope(), _receive, send)

    anyio.run(scenario)
    return answered


def _streaming_middleware() -> tuple[
    CachedResponsesMiddleware, MutableMapping[str, Any]
]:
    """Return a middleware over an app that streams, and a read of it."""

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send(
            {"type": "http.response.body", "body": b"one", "more_body": True}
        )
        await send({"type": "http.response.body", "body": b"two"})

    return (
        CachedResponsesMiddleware(app, cache=_cache(), include={"/reads": TTL}),
        _read_scope(),
    )


def _served_twice(
    handler: Any,  # noqa: ANN401
    component: CachedResponses | None = None,
) -> tuple[Any, Any]:
    """Return both answers to the same read of one handler."""
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            component if component is not None else CachedResponses(),
        ]
    )
    app = FastAPI()
    micro.install(app)
    app.add_api_route(
        "/reads",
        handler,
        methods=["GET"],
        dependencies=[CachedResponse(ttl=TTL)],
    )
    with TestClient(app) as client:
        return client.get("/reads"), client.get("/reads")


def test_a_failure_is_not_stored() -> None:
    """Only `200` is an answer worth handing to somebody else."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"gone", status_code=HTTP_404_NOT_FOUND)

    # Act
    _served_twice(handler)

    # Assert
    assert calls == TWICE


@pytest.mark.parametrize(
    "headers",
    [
        {"Set-Cookie": "session=1"},
        {"Cache-Control": "private"},
        {"Cache-Control": "no-store"},
        {"Cache-Control": "max-age=0, no-cache"},
    ],
    ids=["set-cookie", "private", "no-store", "no-cache"],
)
def test_a_response_that_refuses_the_cache_is_not_stored(
    headers: dict[str, str],
) -> None:
    """Each of these says this response is not one to hand on."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers=headers)

    # Act
    _served_twice(handler)

    # Assert
    assert calls == TWICE


def test_a_compressed_response_is_not_stored() -> None:
    """Compression happens outside this middleware, so what it sees is plain."""
    # Act
    ran = _ran_twice([(b"content-encoding", b"gzip")])

    # Assert
    assert ran == TWICE


def test_a_plain_response_is_stored() -> None:
    """The same harness, to show what the refusals are measured against."""
    # Act
    ran = _ran_twice([])

    # Assert
    assert ran == 1


def test_a_vary_naming_an_undeclared_header_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """This is how a naive cache answers one client with another's response."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers={"Vary": "Accept-Language"})

    # Act
    with caplog.at_level(logging.WARNING, logger="grelmicro.http.cache"):
        _served_twice(handler)

    # Assert
    assert calls == TWICE
    assert "Vary" in caplog.text


def test_a_vary_of_everything_is_refused() -> None:
    """`Vary: *` says no two requests share an answer."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers={"Vary": "*"})

    # Act
    _served_twice(handler)

    # Assert
    assert calls == TWICE


def test_a_declared_vary_is_stored_and_keyed_by_its_header() -> None:
    """One language never answers a request for another."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers={"Vary": "Accept-Language"})

    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(vary_by_headers=("accept-language",)),
        ]
    )
    app = FastAPI()
    micro.install(app)
    app.add_api_route(
        "/reads",
        handler,
        methods=["GET"],
        dependencies=[CachedResponse(ttl=TTL)],
    )

    # Act
    with TestClient(app) as client:
        client.get("/reads", headers={"Accept-Language": "fr"})
        client.get("/reads", headers={"Accept-Language": "fr"})
        client.get("/reads", headers={"Accept-Language": "de"})

    # Assert
    assert calls == TWICE


def test_a_response_the_skip_rule_refuses_is_not_stored() -> None:
    """A route's own rule is the last word on what is kept."""
    # Arrange
    calls = 0
    seen: list[StoredResponse] = []

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok")

    def skip(response: StoredResponse) -> bool:
        seen.append(response)
        return True

    # Act
    _served_twice(handler, CachedResponses(skip=skip))

    # Assert
    assert calls == TWICE
    assert seen[0]["status"] == HTTP_200_OK
    assert seen[0]["body"] == b"ok"


def test_a_body_over_the_limit_is_streamed_and_not_stored() -> None:
    """A large download is never held in memory to keep it."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"x" * BIG)

    # Act
    first, _ = _served_twice(handler, CachedResponses(max_body_size=16))

    # Assert
    assert calls == TWICE
    assert len(first.content) == BIG


def test_a_streamed_response_is_forwarded_as_it_comes() -> None:
    """Holding a stream to keep it would turn it into one message at the end."""
    # Arrange
    calls = 0

    async def chunks() -> AsyncIterator[bytes]:
        yield b"one"
        yield b"two"

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return StreamingResponse(chunks())

    # Act
    first, _ = _served_twice(handler)

    # Assert
    assert calls == TWICE
    assert first.content == b"onetwo"


def test_a_response_the_app_never_finished_is_released() -> None:
    """What the app sent goes out, and nothing half-written is stored."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), include={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]


def test_a_response_declaring_trailers_is_forwarded() -> None:
    """Trailers follow the body, so the response cannot be held and reordered."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
                "trailers": True,
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})
        await send({"type": "http.response.trailers", "headers": []})

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), include={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    assert sent[-1]["type"] == "http.response.trailers"


# --- The key ---


def test_the_query_string_is_part_of_the_key(client: TestClient) -> None:
    """Two different questions are two different answers."""
    # Act
    first = client.get("/reads?page=1")
    second = client.get("/reads?page=2")

    # Assert
    assert first.json() != second.json()


def test_the_order_of_the_query_string_is_not(client: TestClient) -> None:
    """The same question asked in another order is the same question."""
    # Act
    first = client.get("/reads?a=1&b=2")
    second = client.get("/reads?b=2&a=1")

    # Assert
    assert first.json() == second.json()


def test_only_the_named_query_parameters_are_read() -> None:
    """A tracking parameter is not a different resource."""
    # Arrange
    app = _app(CachedResponses(vary_by_query=("page",)))

    # Act
    with TestClient(app) as client:
        first = client.get("/reads?page=1&utm=ad")
        second = client.get("/reads?page=1&utm=mail")

    # Assert
    assert first.json() == second.json()


def test_a_key_builder_replaces_the_key() -> None:
    """A service that knows what makes two requests the same says so."""
    # Arrange
    app = _app(CachedResponses(key=lambda scope: scope["path"]))

    # Act
    with TestClient(app) as client:
        first = client.get("/reads?page=1")
        second = client.get("/reads?page=2")

    # Assert
    assert first.json() == second.json()


def test_a_key_builder_that_returns_none_leaves_the_request_alone() -> None:
    """`None` is how a builder says this one is not cached."""
    # Arrange
    app = _app(CachedResponses(key=lambda _scope: None))

    # Act
    with TestClient(app) as client:
        first = client.get("/reads")
        second = client.get("/reads")

    # Assert
    assert first.json() != second.json()


# --- Folding and purging ---


def test_one_cold_key_runs_the_handler_once() -> None:
    """A cold key must not fan the same computation out to every caller."""
    # Arrange
    calls = 0
    started = anyio.Event()

    async def scenario() -> int:
        from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

        micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
        app = FastAPI()
        micro.install(app)

        @app.get("/reads", dependencies=[CachedResponse(ttl=TTL)])
        async def reads() -> dict[str, int]:
            nonlocal calls
            calls += 1
            started.set()
            await anyio.sleep(0.05)
            return {"calls": calls}

        async with micro:
            transport = ASGITransport(app=app)
            async with (
                AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client,
                anyio.create_task_group() as tasks,
            ):
                for _ in range(5):
                    tasks.start_soon(client.get, "/reads")
        return calls

    # Act
    ran = anyio.run(scenario)

    # Assert
    assert ran == 1


def test_purge_drops_what_the_component_stored() -> None:
    """A write invalidates what the cache would go on answering with."""
    # Arrange
    component = CachedResponses()
    app = _app(component)

    @app.post("/purge")
    async def purge() -> dict[str, bool]:
        await component.purge()
        return {"purged": True}

    # Act
    with TestClient(app) as client:
        first = client.get("/reads")
        client.post("/purge")
        second = client.get("/reads")

    # Assert
    assert first.json() != second.json()


def test_a_response_that_carries_its_own_tag_keeps_it() -> None:
    """A tag the handler set is the resource's, and this adds none over it."""
    # Arrange
    tag = '"seven"'

    async def handler() -> Response:
        return Response(b"ok", headers={"ETag": tag})

    # Act
    first, second = _served_twice(handler)

    # Assert
    assert first.headers["etag"] == second.headers["etag"] == tag


def test_a_path_no_pattern_names_is_left_alone() -> None:
    """A rule that names another path decides nothing about this one."""
    # Arrange
    app = _app(CachedResponses(include={"/elsewhere": TTL}))

    # Act
    with TestClient(app) as client:
        first = client.get("/live")
        second = client.get("/live")

    # Assert
    assert first.json() != second.json()


def test_a_component_that_read_no_app_starts_anyway() -> None:
    """A framework declaring no routes grelmicro can read still runs."""

    # Arrange
    async def scenario() -> str:
        async with CachedResponses() as component:
            return component.name

    # Act / Assert
    assert anyio.run(scenario) == "default"


def test_something_that_is_not_a_route_is_passed_over() -> None:
    """What a router holds is the router's business, not this one's."""
    # Arrange
    component = CachedResponses()

    # Act
    component.read_routes(SimpleNamespace(routes=[object()]))

    # Assert
    assert component.name == "default"


def test_a_mount_of_nothing_carries_no_handler_to_read() -> None:
    """A route with no endpoint has no mark on it."""
    # Arrange
    component = CachedResponses()

    # Act
    component.read_routes(Starlette(routes=[Mount("/empty", routes=[])]))

    # Assert
    assert component.name == "default"


def test_an_app_that_answers_nothing_is_forwarded_as_it_is() -> None:
    """Nothing to hold means nothing to send, and no entry either."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        """Return without answering, as a mounted app that matched nothing."""

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), include={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    assert sent == []


def test_a_message_that_is_neither_start_nor_body_releases_what_is_held() -> (
    None
):
    """Whatever is held goes first, so nothing reaches the client out of order."""
    # Arrange
    sent: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.trailers", "headers": []})

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message["type"])

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), include={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    assert sent == ["http.response.start", "http.response.trailers"]


def test_the_vary_warning_stops_remembering_what_it_warned_about() -> None:
    """A service with many paths must not grow a set of them forever."""
    # Arrange
    middleware = CachedResponsesMiddleware(_nothing, cache=_cache())
    headers = [(b"vary", b"accept-language")]

    # Act
    for index in range(_WARNED_LIMIT + 2):
        middleware._storable(
            headers, state=middleware._live.state, path=f"/p{index}"
        )

    # Assert
    assert len(middleware._warned) <= _WARNED_LIMIT


def test_a_second_cache_control_line_is_read_too() -> None:
    """Reading only the last of them is how the refusal goes missing."""
    # Act
    ran = _ran_twice(
        [(b"cache-control", b"private"), (b"cache-control", b"max-age=60")]
    )

    # Assert
    assert ran == TWICE


def test_a_second_vary_line_is_read_too() -> None:
    """A response says all of what its headers say, not the last of it."""
    # Act
    ran = _ran_twice(
        [(b"vary", b"accept-language"), (b"vary", b"accept-encoding")]
    )

    # Assert
    assert ran == TWICE


@pytest.mark.parametrize("failing", ["get", "set"], ids=["read", "write"])
def test_a_store_that_cannot_be_reached_still_answers(failing: str) -> None:
    """A cache that is down is a cache miss, never a failed request."""
    # Act
    answered = _through_a_broken_store(failing)

    # Assert
    assert answered == [HTTP_200_OK, b"ok"]


def test_a_path_nothing_is_ever_stored_for_stops_taking_the_lock() -> None:
    """A stream would otherwise queue every caller behind the one before."""
    # Arrange
    middleware, scope = _streaming_middleware()

    # Act
    anyio.run(middleware, scope, _receive, _nowhere)

    # Assert
    assert list(middleware._unstorable)


def test_a_handler_that_raises_is_never_swallowed() -> None:
    """A failure inside the fold is the app's, and it travels."""

    # Arrange
    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        msg = "the handler broke"
        raise RuntimeError(msg)

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), include={"/reads": TTL}
    )

    # Act / Assert
    with pytest.raises(RuntimeError, match="the handler broke"):
        anyio.run(middleware, _read_scope(), _receive, _nowhere)


def test_the_unstorable_keys_stop_being_remembered() -> None:
    """A service with many paths must not grow a set of them forever."""
    # Arrange
    middleware = CachedResponsesMiddleware(_nothing, cache=_cache())

    # Act
    for index in range(_UNSTORABLE_LIMIT + 2):
        middleware._remember_unstorable(f"/p{index}")

    # Assert
    assert len(middleware._unstorable) <= _UNSTORABLE_LIMIT


def test_a_path_converter_is_compiled_the_way_the_router_did() -> None:
    """`{rest:path}` matches a nested path, and the rule has to as well."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    micro.install(app)
    calls = 0

    @app.get("/files/{rest:path}", dependencies=[CachedResponse(ttl=TTL)])
    async def files(rest: str) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls, "rest": len(rest)}

    # Act
    with TestClient(app) as client:
        client.get("/files/a/b")
        client.get("/files/a/b")

    # Assert
    assert calls == 1


def test_two_hosts_are_two_resources() -> None:
    """One app answering for two hostnames answers each with its own."""
    # Arrange
    app = _app(CachedResponses())

    # Act
    with TestClient(app) as client:
        first = client.get("/reads", headers={"Host": "a.example.com"})
        second = client.get("/reads", headers={"Host": "b.example.com"})

    # Assert
    assert first.json() != second.json()


@pytest.mark.parametrize("ttl", [0, -1.0], ids=["zero", "negative"])
def test_a_lifetime_a_response_cannot_be_kept_for_is_refused(
    ttl: float,
) -> None:
    """Zero is how a reader writes "not this one", and it is not that."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="number of seconds"):
        CachedResponses(include={"/reads": ttl})
    with pytest.raises(SettingsValidationError, match="number of seconds"):
        CachedResponses(ttl=ttl)
    # The route dependency is not a settings field, so it refuses the way
    # a wrong argument does and names what it was given.
    with pytest.raises(ValueError, match="number of seconds"):
        CachedResponse(ttl=ttl)


def test_a_head_miss_never_takes_the_key_a_read_is_waiting_for() -> None:
    """Folding a `HEAD` would hold a `GET` up for a response nobody keeps."""
    # Arrange
    calls = 0

    async def handler(request: Any) -> Response:  # noqa: ANN401, ARG001
        nonlocal calls
        calls += 1
        return Response(b"ok")

    app = Starlette(routes=[Route("/reads", handler)])
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/reads": TTL}),
        ]
    )
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.head("/reads")
        client.get("/reads")
        client.get("/reads")

    # Assert
    assert calls == TWICE


def test_a_stored_response_survives_a_fold_that_fails_on_its_way_out() -> None:
    """The handler answered, so the caller gets what it answered."""
    # Arrange
    answered: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: MutableMapping[str, Any]) -> None:
        answered.append(message.get("status", message.get("body")))

    async def scenario() -> None:
        async with MemoryCacheAdapter() as backend:
            middleware = CachedResponsesMiddleware(
                app,
                cache=TTLCache(
                    ttl=TTL, backend=backend, serializer=JsonSerializer()
                ),
                include={"/reads": TTL},
            )
            middleware._cache._stampede = _LosingGuard()  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
            await middleware(_read_scope(), _receive, send)

    anyio.run(scenario)

    # Assert
    assert answered == [HTTP_200_OK, b"ok"]


def test_a_response_lost_after_a_fold_that_never_kept_it_is_released() -> None:
    """A stream is not stored, and the lock failing does not lose it either."""
    # Arrange
    sent: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 404, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"gone"})

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message.get("status", message.get("body")))

    async def scenario() -> None:
        async with MemoryCacheAdapter() as backend:
            middleware = CachedResponsesMiddleware(
                app,
                cache=TTLCache(
                    ttl=TTL, backend=backend, serializer=JsonSerializer()
                ),
                include={"/reads": TTL},
            )
            middleware._cache._stampede = _LosingGuard()  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
            await middleware(_read_scope(), _receive, send)

    anyio.run(scenario)

    # Assert
    assert sent == [HTTP_404_NOT_FOUND, b"gone"]


def test_a_stored_response_says_what_the_key_reads() -> None:
    """A CDN in front of this must not hand one caller's copy to another."""
    # Arrange
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(
                include={"/reads": TTL}, vary_by_headers=("accept-language",)
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/reads")
    async def reads() -> dict[str, int]:
        return {"calls": 1}

    # Act
    with TestClient(app) as client:
        response = client.get("/reads")

    # Assert
    assert response.headers["vary"] == "accept-language"


def test_a_vary_the_handler_set_is_kept_beside_it() -> None:
    """What the response says it varies on is not replaced, it is joined."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers={"Vary": "accept-encoding"})

    # Act
    first, _ = _served_twice(
        handler,
        CachedResponses(vary_by_headers=("accept-encoding", "accept-language")),
    )

    # Assert
    assert set(_split(first.headers["vary"])) == {
        "accept-encoding",
        "accept-language",
    }


def test_a_bare_string_is_a_missing_comma() -> None:
    """A string is a sequence of characters, and it fails silently."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="is a string"):
        CachedResponses(vary_by_headers="accept-language")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(SettingsValidationError, match="is a string"):
        CachedResponses(vary_by_query="page")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(SettingsValidationError, match="is a string"):
        CachedResponses(include="/reads")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    # Hand-wired ASGI refuses the same mistake as a wrong argument type.
    with pytest.raises(TypeError, match="is a string"):
        CachedResponsesMiddleware(_nothing, cache=_cache(), include="/reads")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_two_services_behind_one_gateway_are_two_resources() -> None:
    """One store shared by two deployments must not cross-serve."""
    # Arrange
    middleware = CachedResponsesMiddleware(_nothing, cache=_cache())
    orders = _read_scope()
    orders["root_path"] = "/orders"
    billing = _read_scope()
    billing["root_path"] = "/billing"

    # Act
    keys = {
        middleware._built(middleware._live.state, orders, "/reads"),
        middleware._built(middleware._live.state, billing, "/reads"),
    }

    # Assert
    assert len(keys) == TWICE


def test_the_declaration_is_resolved_on_the_event_loop() -> None:
    """A sync dependency costs a worker thread, and this one does nothing."""
    # Act
    declared = declare_cached(TTL)

    # Assert
    assert iscoroutinefunction(declared)


def test_an_included_router_is_read() -> None:
    """Most apps are built out of routers, and a rule on one has to count."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    router = APIRouter(prefix="/v1")
    calls = 0

    @router.get("/reads", dependencies=[CachedResponse(ttl=TTL)])
    async def reads() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app.include_router(router, prefix="/api")
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/api/v1/reads")
        client.get("/api/v1/reads")

    # Assert
    assert calls == 1


def test_a_router_declares_it_for_everything_under_it() -> None:
    """`include_router(dependencies=[...])` is how a whole router says so."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    router = APIRouter()
    calls = 0

    @router.get("/reads")
    async def reads() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app.include_router(
        router, prefix="/api", dependencies=[CachedResponse(ttl=TTL)]
    )
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/api/reads")
        client.get("/api/reads")

    # Assert
    assert calls == 1


def test_a_write_inside_a_router_is_refused_too() -> None:
    """The guard has to reach the routes an included router holds."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    router = APIRouter()

    @router.post("/orders", dependencies=[CachedResponse(ttl=TTL)])
    async def create() -> dict[str, int]:
        return {"id": 1}

    app.include_router(router, prefix="/api")

    # Act / Assert
    with pytest.raises(TypeError, match="answers POST"):
        micro.install(app)


def test_a_route_behind_a_security_scheme_is_refused() -> None:
    """A hit answers before the app is routed, so the gate would not run."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    key = APIKeyHeader(name="X-API-Key")

    @app.get(
        "/secret",
        dependencies=[Security(key), CachedResponse(ttl=TTL)],
    )
    async def secret() -> dict[str, int]:
        return {"secret": 1}

    # Act / Assert
    with pytest.raises(TypeError, match="gated by APIKeyHeader"):
        micro.install(app)


def test_a_router_gate_is_refused_like_a_route_one() -> None:
    """An include's gate never runs on a hit either."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    router = APIRouter()

    @router.get("/secret", dependencies=[CachedResponse(ttl=TTL)])
    async def secret() -> dict[str, int]:
        return {"secret": 1}

    app.include_router(
        router, dependencies=[Security(APIKeyHeader(name="X-API-Key"))]
    )

    # Act / Assert
    with pytest.raises(TypeError, match="gated by APIKeyHeader"):
        micro.install(app)


def test_a_router_declaration_leaves_what_it_cannot_answer_alone() -> None:
    """A router holds more than reads, and a write is not one to refuse."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    router = APIRouter()
    reads = 0
    writes = 0

    @router.get("/items")
    async def list_items() -> dict[str, int]:
        nonlocal reads
        reads += 1
        return {"reads": reads}

    @router.post("/items")
    async def create() -> dict[str, int]:
        nonlocal writes
        writes += 1
        return {"writes": writes}

    app.include_router(router, dependencies=[CachedResponse(ttl=TTL)])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/items")
        client.get("/items")
        client.post("/items")
        client.post("/items")

    # Assert
    assert reads == 1
    assert writes == TWICE


def test_the_nearest_router_declaration_decides() -> None:
    """Every other override here goes to the more specific one."""
    # Arrange
    component = CachedResponses()
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), component])
    app = FastAPI()
    inner = APIRouter()

    @inner.get("/x")
    async def x() -> dict[str, int]:
        return {"x": 1}

    middle = APIRouter()
    middle.include_router(inner, dependencies=[CachedResponse(ttl=TTL)])
    app.include_router(middle, dependencies=[CachedResponse(ttl=OTHER_TTL)])
    micro.install(app)

    # Act
    seconds = component._policies.ttl_for("/x", OTHER_TTL)

    # Assert
    assert seconds == TTL


def test_a_gate_inside_a_router_dependency_is_refused_too() -> None:
    """A scheme is a scheme however deep the dependency that declares it."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    router = APIRouter()

    async def require_key(
        key: str = Security(APIKeyHeader(name="X-API-Key")),
    ) -> str:
        """Stand in for the gate a service writes once and reuses."""
        return key

    @router.get("/secret", dependencies=[CachedResponse(ttl=TTL)])
    async def secret() -> dict[str, int]:
        return {"secret": 1}

    app.include_router(router, dependencies=[Depends(require_key)])

    # Act / Assert
    with pytest.raises(TypeError, match="gated by APIKeyHeader"):
        micro.install(app)


def test_a_declaration_with_nothing_to_call_gates_nothing() -> None:
    """What names no callable declares no scheme to walk."""
    # Act
    schemes = _declared_schemes(
        SimpleNamespace(dependencies=[SimpleNamespace(dependency=None)])
    )

    # Assert
    assert schemes == []


async def _require_key(
    key: str = Security(APIKeyHeader(name="X-API-Key")),
) -> str:
    """Stand in for the gate a service writes once and reuses."""
    return key


async def _through_a_gate(key: str = Depends(_require_key)) -> str:
    """Stand in for a gate reached through another one."""
    return key


def _cached_router(dependencies: list[Any] | None = None) -> APIRouter:
    """Return a router holding one read that asks to be cached."""
    router = APIRouter(dependencies=dependencies)

    @router.get("/secret", dependencies=[CachedResponse(ttl=TTL)])
    async def secret() -> dict[str, int]:
        return {"secret": 1}

    return router


def _gated_app(spelling: str) -> FastAPI:
    """Return an app whose cached read is gated, spelled one of seven ways."""
    app = FastAPI()
    gate = Security(APIKeyHeader(name="X-API-Key"))
    if spelling == "on the route":
        app.add_api_route(
            "/secret",
            lambda: {"secret": 1},
            methods=["GET"],
            dependencies=[gate, CachedResponse(ttl=TTL)],
        )
    elif spelling == "on the include":
        app.include_router(_cached_router(), dependencies=[gate])
    elif spelling == "through the include":
        app.include_router(
            _cached_router(), dependencies=[Depends(_require_key)]
        )
    elif spelling == "two deep":
        app.include_router(
            _cached_router(), dependencies=[Depends(_through_a_gate)]
        )
    elif spelling == "on the router":
        app.include_router(_cached_router([gate]))
    elif spelling == "on the app":
        app = FastAPI(dependencies=[gate])
        app.include_router(_cached_router())
    elif spelling == "nested":  # pragma: no branch
        middle = APIRouter()
        middle.include_router(_cached_router(), dependencies=[gate])
        app.include_router(middle, prefix="/api")
    return app


@pytest.mark.parametrize(
    "spelling",
    [
        "on the route",
        "on the include",
        "through the include",
        "two deep",
        "on the router",
        "on the app",
        "nested",
    ],
)
def test_a_gated_read_is_refused_however_the_gate_is_spelled(
    spelling: str,
) -> None:
    """A hit answers before the app is routed, so no gate would run.

    The whole surface, because each spelling reaches the route by a road
    of its own, and one that is not read is a cached response handed to
    whoever asks for it next.
    """
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = _gated_app(spelling)

    # Act / Assert
    with pytest.raises(TypeError, match="gated by APIKeyHeader"):
        micro.install(app)


def test_an_app_built_with_it_leaves_its_writes_alone() -> None:
    """`FastAPI(dependencies=[...])` says the same as a router does."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI(dependencies=[CachedResponse(ttl=TTL)])
    reads = 0
    writes = 0

    @app.get("/items")
    async def list_items() -> dict[str, int]:
        nonlocal reads
        reads += 1
        return {"reads": reads}

    @app.post("/items")
    async def create() -> dict[str, int]:
        nonlocal writes
        writes += 1
        return {"writes": writes}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/items")
        client.get("/items")
        client.post("/items")
        client.post("/items")

    # Assert
    assert reads == 1
    assert writes == TWICE


def test_a_pattern_naming_a_gated_read_is_refused_too() -> None:
    """`include=` reaches the same route by another road, and the same gate."""
    # Arrange
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/admin/*": TTL}),
        ]
    )
    app = FastAPI()
    router = APIRouter(prefix="/admin")

    @router.get("/reports")
    async def reports() -> dict[str, int]:
        return {"reports": 1}

    app.include_router(
        router, dependencies=[Security(APIKeyHeader(name="X-API-Key"))]
    )

    # Act / Assert
    with pytest.raises(TypeError, match="include= names"):
        micro.install(app)


def test_a_pattern_naming_a_gated_write_is_left_alone() -> None:
    """A write is never answered from the cache, so it is not the mistake."""
    # Arrange
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/admin/*": TTL}),
        ]
    )
    app = FastAPI()
    router = APIRouter(prefix="/admin")

    @router.post("/reports")
    async def create() -> dict[str, int]:
        return {"reports": 1}

    app.include_router(
        router, dependencies=[Security(APIKeyHeader(name="X-API-Key"))]
    )

    # Act
    micro.install(app)

    # Assert
    with TestClient(app) as client:
        assert client.post(
            "/admin/reports", headers={"X-API-Key": "k"}
        ).json() == {"reports": 1}


def test_a_pattern_naming_an_open_read_is_left_alone() -> None:
    """Nothing stands in front of it, so the pattern is what it says."""
    # Arrange
    app = _app(CachedResponses(include={"/live": TTL}))

    # Act
    with TestClient(app) as client:
        first = client.get("/live")
        second = client.get("/live")

    # Assert
    assert first.json() == second.json()


def test_the_smallest_shared_lifetime_wins() -> None:
    """Every occurrence counts, and the most conservative one decides."""
    # Arrange
    middleware = CachedResponsesMiddleware(_nothing, cache=_cache())

    # Act
    seconds = middleware._storable(
        [
            (b"cache-control", b"s-maxage=100"),
            (b"cache-control", b"s-maxage=50"),
        ],
        state=middleware._live.state,
        path="/reads",
    )

    # Assert
    assert seconds == SHARED_TTL


def test_a_router_built_with_it_leaves_its_writes_alone() -> None:
    """`APIRouter(dependencies=[...])` says the same as including with it."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    router = APIRouter(
        prefix="/products", dependencies=[CachedResponse(ttl=TTL)]
    )
    reads = 0
    writes = 0

    @router.get("/")
    async def list_products() -> dict[str, int]:
        nonlocal reads
        reads += 1
        return {"reads": reads}

    @router.post("/")
    async def create() -> dict[str, int]:
        nonlocal writes
        writes += 1
        return {"writes": writes}

    app.include_router(router)
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/products/")
        client.get("/products/")
        client.post("/products/")
        client.post("/products/")

    # Assert
    assert reads == 1
    assert writes == TWICE


def test_a_route_that_declared_one_is_not_overridden_by_a_pattern() -> None:
    """`include=` fills in for the routes that declared none."""
    # Arrange
    component = CachedResponses(include={"/products/*": TTL})
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), component])
    app = FastAPI()

    @app.get("/products/hot", dependencies=[CachedResponse(ttl=OTHER_TTL)])
    async def hot() -> dict[str, int]:
        return {"hot": 1}

    micro.install(app)

    # Act
    seconds = component._policies.ttl_for("/products/hot", TTL)

    # Assert
    assert seconds == OTHER_TTL


def test_a_message_after_a_complete_body_closes_what_is_held() -> None:
    """A response held open is one the client waits on for ever."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"hello"})
        await send({"type": "http.response.trailers", "headers": []})

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), include={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    body = [message for message in sent if "body" in message]
    assert [message.get("more_body", False) for message in body] == [False]


def test_a_range_request_is_left_to_the_handler() -> None:
    """A stored whole is not the part the client asked for."""
    # Arrange
    app = _app(CachedResponses())

    # Act
    with TestClient(app) as client:
        client.get("/reads")
        ranged = client.get("/reads", headers={"Range": "bytes=0-3"})

    # Assert
    assert ranged.json() == {"calls": TWICE}


def test_a_response_is_kept_no_longer_than_it_says() -> None:
    """`max-age` is the handler saying how long this answer is good for."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers={"Cache-Control": "max-age=0"})

    # Act
    _served_twice(handler)

    # Assert
    assert calls == TWICE


@pytest.mark.parametrize(
    ("directive", "kept"),
    [("max-age=30", 30.0), ("s-maxage=10, max-age=99", 10.0), ("public", None)],
    ids=["max-age", "s-maxage", "neither"],
)
def test_the_freshness_a_response_names_caps_how_long_it_is_kept(
    directive: str, kept: float | None
) -> None:
    """A shared cache reads `s-maxage` first, because it is written for it."""
    # Arrange
    middleware = CachedResponsesMiddleware(_nothing, cache=_cache())

    # Act
    seconds = middleware._storable(
        [(b"cache-control", directive.encode())],
        state=middleware._live.state,
        path="/reads",
    )

    # Assert
    assert seconds == (math.inf if kept is None else kept)


def test_a_router_that_declares_something_else_is_passed_over() -> None:
    """A router gate is not a cache rule, and reading it as one would cache."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    router = APIRouter()
    calls = 0

    async def audit() -> None:
        """Stand in for the gate a router already declares."""

    @router.get("/reads")
    async def reads() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app.include_router(router, dependencies=[Depends(audit)])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/reads")
        client.get("/reads")

    # Assert
    assert calls == TWICE


def test_a_freshness_that_is_not_a_number_is_passed_over() -> None:
    """A header nobody can read decides nothing about how long it is kept."""
    # Arrange
    middleware = CachedResponsesMiddleware(_nothing, cache=_cache())

    # Act
    seconds = middleware._storable(
        [(b"cache-control", b"max-age=soon")],
        state=middleware._live.state,
        path="/reads",
    )

    # Assert
    assert seconds == math.inf


def test_a_store_that_is_down_does_not_become_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One report per reason a minute, not one traceback per request."""
    # Act
    with caplog.at_level(logging.WARNING, logger="grelmicro.http.cache"):
        _through_a_broken_store("get", reads=READS)

    # Assert
    assert len(caplog.records) == TWICE


def test_the_most_specific_pattern_decides() -> None:
    """A rule written for one route is not answered by its router's."""
    # Arrange
    component = CachedResponses(
        include={"/products/*": TTL, "/products/hot": OTHER_TTL}
    )

    # Act
    seconds = component._policies.ttl_for("/products/hot", TTL)

    # Assert
    assert seconds == OTHER_TTL


def test_the_cache_it_stores_in_is_readable() -> None:
    """An operator asking what is kept reaches the store the component holds."""
    # Arrange
    own = _cache()

    # Act
    component = CachedResponses(cache=own)

    # Assert
    assert component.cache is own
    assert component.name == "default"
